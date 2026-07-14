"""
Data Cloud(Data 360) Data Stream 1회성 Full Refresh 실행 스크립트.

전제:
- .env 파일에 SF_LOGIN_URL, SF_CLIENT_ID, SF_CLIENT_SECRET 필요
  (Username-Password Flow라면 SF_USERNAME, SF_PASSWORD_AND_TOKEN도 필요)
- 대상 데이터 스트림의 recordId 또는 name(developer name)

동작:
1. Salesforce 코어 OAuth 토큰 발급 (client_credentials 또는 password grant)
2. 대상 스트림의 refreshConfig.refreshMode 를 TOTAL_REPLACE 로 PATCH
3. 실행 트리거 POST .../actions/run
4. 성공 여부와 무관하게 마지막에 refreshMode 를 UPSERT 로 되돌림 (--no-revert 로 끄지 않는 한)

주의:
- ssot/data-streams 관리 API(PATCH/run)는 Data Cloud 전용 토큰 교환(/services/a360/token) 없이
  코어 org 도메인 + 코어 액세스 토큰으로 호출해야 함 (DC 토큰 교환은 Query API 전용).
- run 엔드포인트 경로(POST /ssot/data-streams/{id}/actions/run)는
  calculated-insights의 동일 패턴에서 유추한 것으로, 404가 나면 실제 경로가 다를 수 있음.
"""
import argparse
import os
import sys
import time

import requests
from dotenv import load_dotenv

API_VERSION = "v66.0"


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


def set_refresh_mode(dc_instance_url, dc_token, stream_id, mode):
    url = f"{dc_instance_url}/services/data/{API_VERSION}/ssot/data-streams/{stream_id}"
    headers = {"Authorization": f"Bearer {dc_token}", "Content-Type": "application/json"}
    body = {"refreshConfig": {"refreshMode": mode}}
    resp = requests.patch(url, headers=headers, json=body)
    print(f"  PATCH refreshMode={mode} -> {resp.status_code}")
    if not resp.ok:
        print(f"  응답 본문: {resp.text}")
    resp.raise_for_status()
    return resp.json() if resp.text else None


def wait_until_active(dc_instance_url, dc_token, stream_id, attempts=6, delay=3):
    url = f"{dc_instance_url}/services/data/{API_VERSION}/ssot/data-streams/{stream_id}"
    headers = {"Authorization": f"Bearer {dc_token}"}
    for _ in range(attempts):
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        status = resp.json().get("status")
        if status == "ACTIVE":
            return
        time.sleep(delay)
    print(f"  경고: {attempts * delay}초 대기했지만 status가 ACTIVE로 확정되지 않음 (마지막 status={status})")


def run_stream(dc_instance_url, dc_token, stream_id):
    url = f"{dc_instance_url}/services/data/{API_VERSION}/ssot/data-streams/{stream_id}/actions/run"
    headers = {"Authorization": f"Bearer {dc_token}"}
    resp = requests.post(url, headers=headers)
    print(f"  POST run -> {resp.status_code}")
    if not resp.ok:
        print(f"  응답 본문: {resp.text}")
    resp.raise_for_status()
    return resp.json() if resp.text else None


def main():
    parser = argparse.ArgumentParser(description="Data Cloud 데이터 스트림 1회성 Full Refresh")
    parser.add_argument("--stream-id", required=True, help="대상 데이터 스트림 recordId 또는 name")
    parser.add_argument("--no-revert", action="store_true", help="완료 후 UPSERT로 되돌리지 않음")
    args = parser.parse_args()

    load_dotenv()
    login_url = os.environ["SF_LOGIN_URL"].rstrip("/")
    client_id = os.environ["SF_CLIENT_ID"]
    client_secret = os.environ["SF_CLIENT_SECRET"]
    username = os.environ.get("SF_USERNAME") or None
    password_and_token = os.environ.get("SF_PASSWORD_AND_TOKEN") or None

    print("[1/3] Salesforce 코어 토큰 발급 중...")
    core = get_core_token(login_url, client_id, client_secret, username, password_and_token)
    core_instance_url = core["instance_url"]
    core_token = core["access_token"]
    print(f"  core instance_url = {core_instance_url}")
    print("  (ssot/data-streams 관리 API는 DC 토큰 교환 없이 코어 토큰 + 코어 도메인으로 호출)")

    stream_id = args.stream_id
    try:
        print(f"[2/3] refreshMode를 TOTAL_REPLACE로 변경 -> 실행 트리거 ({stream_id})")
        set_refresh_mode(core_instance_url, core_token, stream_id, "TOTAL_REPLACE")
        wait_until_active(core_instance_url, core_token, stream_id)
        run_stream(core_instance_url, core_token, stream_id)
        print("  실행 트리거 완료. Data Cloud UI에서 진행 상태를 확인하세요.")
    finally:
        if not args.no_revert:
            print("[3/3] refreshMode를 UPSERT로 원복")
            wait_until_active(core_instance_url, core_token, stream_id)
            set_refresh_mode(core_instance_url, core_token, stream_id, "UPSERT")
        else:
            print("[3/3] --no-revert 지정됨: refreshMode 원복 생략")


if __name__ == "__main__":
    main()
