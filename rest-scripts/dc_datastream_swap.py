"""
Data Cloud(Data 360) 데이터 스트림 임시 변경 → 실행 → 원상복구 스크립트.

시나리오:
1. GET   : 대상 스트림의 현재 상태를 조회하고 원본 값(디렉토리/파일명/refreshMode)을 저장
2. UPDATE: importDirectory / fileName / refreshMode 를 목표값으로 PATCH
3. RUN   : actions/run 으로 1회 실행 트리거
4. UPDATE: 저장해둔 원본 값으로 되돌림 (원상복구) — run 성공/실패와 무관하게 finally에서 항상 수행

전제:
- .env 에 SF_LOGIN_URL, SF_CLIENT_ID, SF_CLIENT_SECRET (password flow면 SF_USERNAME, SF_PASSWORD_AND_TOKEN)
- ssot/data-streams 관리 API는 DC 토큰 교환 없이 코어 org 도메인 + 코어 액세스 토큰으로 호출.

주의:
- GET 응답의 필드명이 UPDATE 요청의 필드명과 다를 수 있음(문서 예시가 커넥터 타입별로 다름).
  → 쓰기(PATCH)는 UPDATE 스펙 이름(importDirectory/fileName/refreshMode)을 사용.
  → 읽기(원본 저장)는 그 이름을 먼저 찾고, 없으면 유사 키를 탐지해서 함께 출력하므로,
    --get-only 로 먼저 실제 응답 구조를 확인한 뒤 본 실행하는 것을 권장.
- run 엔드포인트 경로(POST .../actions/run)는 404가 나면 실제 경로가 다를 수 있음.
- PATCH가 partial 업데이트를 받는다는 가정(바꾸는 3개 필드만 전송). 전체 body가 필요하면 실행 시 에러로 드러남.
"""
import argparse
import copy
import json
import os
import sys
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

API_VERSION = "v66.0"

# UPDATE(PATCH) 시 쓰는 필드명 — 문서의 update request 샘플 기준
DIR_KEY = "importDirectory"
FILE_KEY = "fileName"


def get_core_token(login_url, client_id, client_secret, username=None, password_and_token=None):
    url = f"{login_url}/services/oauth2/token"
    if username and password_and_token:
        data = {
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": username,
            "password": password_and_token,
        }
    else:
        data = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    resp = requests.post(url, data=data)
    resp.raise_for_status()
    return resp.json()


def get_stream(instance_url, token, stream_id):
    url = f"{instance_url}/services/data/{API_VERSION}/ssot/data-streams/{stream_id}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers)
    print(f"  GET data-stream -> {resp.status_code}")
    if not resp.ok:
        print(f"  응답 본문: {resp.text}")
    resp.raise_for_status()
    return resp.json()


def patch_stream(instance_url, token, stream_id, body):
    url = f"{instance_url}/services/data/{API_VERSION}/ssot/data-streams/{stream_id}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.patch(url, headers=headers, json=body)
    print(f"  PATCH -> {resp.status_code}  body={json.dumps(body, ensure_ascii=False)}")
    if not resp.ok:
        print(f"  응답 본문: {resp.text}")
    resp.raise_for_status()
    return resp.json() if resp.text else None


def wait_until_active(instance_url, token, stream_id, attempts=6, delay=3):
    for _ in range(attempts):
        stream = get_stream(instance_url, token, stream_id)
        status = stream.get("status") or stream.get("dataLakeObjectInfo", {}).get("status")
        if status == "ACTIVE":
            return
        time.sleep(delay)
    print(f"  경고: {attempts * delay}초 대기했지만 status가 ACTIVE로 확정되지 않음 (마지막 status={status})")


def run_stream(instance_url, token, stream_id):
    url = f"{instance_url}/services/data/{API_VERSION}/ssot/data-streams/{stream_id}/actions/run"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(url, headers=headers)
    print(f"  POST run -> {resp.status_code}")
    if not resp.ok:
        print(f"  응답 본문: {resp.text}")
    resp.raise_for_status()
    return resp.json() if resp.text else None


def wait_for_run_complete(instance_url, token, stream_id, prev_refresh_date, timeout=900, interval=15):
    """run 트리거 후, lastRefreshDate가 새 값으로 바뀔 때까지(=refresh 잡 완료) 대기.
    이걸 기다린 뒤에 원복해야 TOTAL_REPLACE 설정 하에서 잡이 실제로 실행됨.
    반환: 완료 감지 시 최신 stream dict, 타임아웃 시 None."""
    deadline = time.time() + timeout
    print(f"  run 완료 대기 중... (직전 lastRefreshDate={prev_refresh_date}, timeout={timeout}s)")
    while time.time() < deadline:
        time.sleep(interval)
        stream = get_stream(instance_url, token, stream_id)
        rd = stream.get("lastRefreshDate")
        rs = stream.get("lastRunStatus")
        proc = stream.get("lastProcessedRecords")
        if rd and rd != prev_refresh_date:
            print(f"  ✅ 새 refresh 완료 감지: lastRefreshDate={rd}, status={rs}, processed={proc}")
            return stream
        print(f"    ...아직 (lastRefreshDate={rd}, status={rs})")
    print(f"  ⚠️ 타임아웃({timeout}s): run 완료를 확인 못 함. 그래도 원복은 진행합니다.")
    return None


def _find_fuzzy(obj, needles):
    """중첩 dict에서 needles(소문자 부분문자열 전부 포함) 키를 찾아 (경로, 값) 목록 반환."""
    hits = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                kl = k.lower()
                if all(n in kl for n in needles) and not isinstance(v, (dict, list)):
                    hits.append((path + [k], v))
                walk(v, path + [k])

    walk(obj, [])
    return hits


def extract_current(stream):
    """원본 값 저장. UPDATE 이름을 우선 찾고, 없으면 유사 키를 탐지해 리포트."""
    adv = stream.get("advancedAttributes") or {}
    refresh = stream.get("refreshConfig") or {}

    current = {
        "refreshMode": refresh.get("refreshMode"),
        DIR_KEY: adv.get(DIR_KEY),
        FILE_KEY: adv.get(FILE_KEY),
    }

    print("\n  [현재 값 감지]")
    print(f"    refreshMode           = {current['refreshMode']!r}  (refreshConfig.refreshMode)")
    print(f"    {DIR_KEY:<21} = {current[DIR_KEY]!r}  (advancedAttributes.{DIR_KEY})")
    print(f"    {FILE_KEY:<21} = {current[FILE_KEY]!r}  (advancedAttributes.{FILE_KEY})")

    if current[DIR_KEY] is None:
        for path, val in _find_fuzzy(stream, ["directory"]):
            print(f"    ↳ 디렉토리 후보: {'.'.join(path)} = {val!r}")
    if current[FILE_KEY] is None:
        for path, val in _find_fuzzy(stream, ["file", "name"]):
            print(f"    ↳ 파일명 후보:   {'.'.join(path)} = {val!r}")

    return current


def build_patch(refresh_mode=None, directory=None, file_name=None):
    body = {}
    if refresh_mode is not None:
        body["refreshConfig"] = {"refreshMode": refresh_mode}
    adv = {}
    if directory is not None:
        adv[DIR_KEY] = directory
    if file_name is not None:
        adv[FILE_KEY] = file_name
    if adv:
        body["advancedAttributes"] = adv
    return body


def main():
    parser = argparse.ArgumentParser(description="Data Cloud 데이터 스트림 임시 변경→실행→원복")
    parser.add_argument("--stream-id", required=True, help="대상 스트림 recordId 또는 developer name")
    parser.add_argument("--dir", dest="directory", help="변경할 importDirectory 값")
    parser.add_argument("--orig-dir", dest="orig_dir",
                        help="복원용 원본 importDirectory 값 (GET으로 못 읽으므로 --dir 사용 시 필수). "
                             "Data Cloud UI의 스트림 설정에서 확인.")
    parser.add_argument("--file", dest="file_name", help="변경할 fileName 값")
    parser.add_argument("--mode", default="TOTAL_REPLACE", help="변경할 refreshMode (기본 TOTAL_REPLACE)")
    parser.add_argument("--get-only", action="store_true", help="GET만 하고 현재 구조/값만 출력 후 종료")
    parser.add_argument("--no-run", action="store_true", help="UPDATE만 하고 run 트리거는 생략")
    parser.add_argument("--no-wait-run", action="store_true",
                        help="run 완료를 기다리지 않고 바로 원복 (비동기 race로 full refresh가 안 먹을 수 있음 — 비권장)")
    parser.add_argument("--run-timeout", type=int, default=900, help="run 완료 대기 최대 초 (기본 900)")
    parser.add_argument("--no-revert", action="store_true", help="완료 후 원본으로 되돌리지 않음")
    args = parser.parse_args()

    load_dotenv()
    login_url = os.environ["SF_LOGIN_URL"].rstrip("/")
    client_id = os.environ["SF_CLIENT_ID"]
    client_secret = os.environ["SF_CLIENT_SECRET"]
    username = os.environ.get("SF_USERNAME") or None
    password_and_token = os.environ.get("SF_PASSWORD_AND_TOKEN") or None

    print("[1] 코어 토큰 발급 중...")
    core = get_core_token(login_url, client_id, client_secret, username, password_and_token)
    instance_url = core["instance_url"]
    token = core["access_token"]
    print(f"  instance_url = {instance_url}")

    stream_id = args.stream_id

    print(f"[2] 현재 상태 GET ({stream_id})")
    original = get_stream(instance_url, token, stream_id)

    # 원본 raw 백업 (중간에 죽어도 수동 복원 가능)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"datastream_backup_{stream_id}_{ts}.json"
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(original, f, ensure_ascii=False, indent=2)
    print(f"  원본 백업 저장: {backup_path}")

    current = extract_current(original)

    if args.get_only:
        print("\n[get-only] 변경 없이 종료. 위 감지 결과로 필드명이 맞는지 확인하세요.")
        return

    # 디렉토리는 GET으로 현재값을 못 읽으므로, --dir로 바꾸고 원복하려면 --orig-dir 필수.
    if args.directory is not None and not args.no_revert and args.orig_dir is None:
        sys.exit("에러: --dir로 디렉토리를 바꾸는데 원복할 --orig-dir 이 없습니다. "
                 "(GET 응답에 importDirectory가 없어 원본을 자동 저장할 수 없음). "
                 "원본 디렉토리 값을 --orig-dir 로 주거나, --no-revert 로 원복을 끄세요.")

    revert_body = build_patch(
        refresh_mode=current["refreshMode"] if args.mode is not None else None,
        directory=args.orig_dir if args.directory is not None else None,
        file_name=current[FILE_KEY] if args.file_name is not None else None,
    )

    try:
        print("[3] 목표값으로 UPDATE")
        update_body = build_patch(
            refresh_mode=args.mode,
            directory=args.directory,
            file_name=args.file_name,
        )
        if not update_body:
            print("  변경할 값이 없습니다(--dir/--file/--mode 중 하나 필요). 종료.")
            return
        patch_stream(instance_url, token, stream_id, update_body)

        if not args.no_run:
            print("[4] 실행 트리거 (run)")
            wait_until_active(instance_url, token, stream_id)
            prev_refresh_date = original.get("lastRefreshDate")
            run_stream(instance_url, token, stream_id)
            if not args.no_wait_run:
                # 중요: run 잡이 TOTAL_REPLACE 설정 하에 실제로 완료될 때까지 대기한 뒤 원복.
                # 안 기다리고 원복하면 비동기 잡이 원복된 UPSERT 설정으로 돌아버림.
                wait_for_run_complete(instance_url, token, stream_id, prev_refresh_date,
                                      timeout=args.run_timeout)
            else:
                print("  --no-wait-run 지정: run 완료를 안 기다리고 원복 진행 (full refresh 누락 위험)")
        else:
            print("[4] --no-run 지정: run 생략")
    finally:
        if args.no_revert:
            print("[5] --no-revert 지정: 원복 생략")
            print(f"    수동 복원 필요 시 백업 파일 참고: {backup_path}")
        elif revert_body:
            print("[5] 원본 값으로 원복")
            wait_until_active(instance_url, token, stream_id)
            try:
                patch_stream(instance_url, token, stream_id, revert_body)
                print("  원복 완료.")
            except Exception as e:
                print(f"  ❌ 원복 실패: {e}")
                print(f"    수동 복원 필요. 백업 파일: {backup_path}")
                raise
        else:
            print("[5] 원복할 원본 값이 없어 생략.")


if __name__ == "__main__":
    main()
