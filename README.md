# Data Stream Mode Switcher

Salesforce **Data Cloud(Data 360)** 데이터 스트림 하나의 설정(refresh mode · 소스 디렉토리 · 파일명)을 **하루 2번 자동 전환**하는 Scheduled Apex. 한 스트림으로 **daily 증분(UPSERT)** 과 **monthly 전체 교체(TOTAL_REPLACE)** 를 모두 처리한다. 외부 인프라 없이 org 안(Scheduled Apex + Named Credential)에서 동작하며 코드에 시크릿이 없다.

## Apex 클래스

**`DataStreamModeFlipper`** (`Schedulable`) — 프로파일(`DAILY`/`MONTHLY`)을 받아 스트림 config 를 전환한다.

- `execute()` → `Queueable`(`FlipJob`) 로 위임 (콜아웃 분리)
- `FlipJob` 동작:
  1. **GET** 으로 스트림 `status` 확인
  2. `ACTIVE`/`Error`/`Inactive` 면 **PATCH** 로 `refreshMode` · `importDirectory` · `fileName` 전환
  3. 그 외(처리중)면 **5분 뒤 재시도** (`AsyncOptions` 지연 큐, 최대 10회) — 처리중 스트림은 PATCH 가 400 으로 거부되기 때문
- 전환 값은 **`Data_Stream_Profile__mdt`** (Custom Metadata) 의 `DAILY`/`MONTHLY` 레코드에서 읽음 → 재배포 없이 값 변경
- 인증은 **Named Credential `DataCloud_Org`** (시크릿·토큰 처리는 플랫폼이 담당)

## 개요 — 왜 필요했나

데이터 스트림 1개는 **(디렉토리 · 파일명 · refresh mode · 스케줄) 조합을 하나만** 가진다. 그런데 적재 요구는 둘이었다:

| | 디렉토리 | 파일명 | 모드 | 업로드 |
|---|---|---|---|---|
| daily | `hot-daily` | `hot-daily_*.csv` | `UPSERT` (증분) | 08:00~22:00, 30분 간격 |
| monthly | `hot-monthly` | `hot-monthly_*.csv` | `TOTAL_REPLACE` (전체 교체) | 매일 00:30, 마스터 스냅샷 |

- native 스케줄 하나로는 둘을 동시에 못 담는다.
- 게다가 데이터 스트림은 **"마지막 run 일시" 워터마크**로 파일을 거르는데, 이 워터마크는 **mode 무관·스트림당 공유**라 run 단위로 config 를 바꿔치기하면 daily 파일이 누락된다 → run 단위 swap 불가.

**해결**: 스트림의 native 30분 스케줄은 그대로 두고, **config 만 하루 2번 flip**(각 config 를 몇 시간 유지 → 워터마크·비동기 문제 회피).

## 테스트 결과

> _직접 작성 예정 (스크린샷 첨부)_
>
> - MONTHLY flip → 스트림이 `TOTAL_REPLACE` / `hot-monthly` 로 전환
> - DAILY flip → `UPSERT` / `hot-daily` 로 복귀
> - 스케줄 자동 실행 (00:15 / 03:00 KST)

## 아키텍처

```
Scheduled Apex (하루 2번)             Named Credential
  00:15 → FlipJob('MONTHLY')  ──▶  callout:DataCloud_Org  ──▶  PATCH /ssot/data-streams/{id}
  03:00 → FlipJob('DAILY')          (OAuth 토큰 자동 주입)         { refreshConfig, advancedAttributes }
      │
      └─ 전환 값은 Data_Stream_Profile__mdt (Custom Metadata) 에서 읽음
```

하루 사이클:

| 시각(KST) | config | 동작 |
|---|---|---|
| 08:00~22:00 | DAILY (UPSERT) | native 30분 스케줄이 daily 파일 증분 적재 |
| **00:15** | → MONTHLY flip | 이후 monthly 폴더 바라봄 |
| 00:30 | MONTHLY | monthly 파일 도착 → 다음 run 이 전체 교체 |
| **03:00** | → DAILY flip | daily 복귀 (처리중이면 자동 재시도) |

## 설정

Salesforce Setup(Connected App · External/Named Credential · Permission Set) → 배포 → 스케줄 등록까지 전 과정은 **[SETUP.md](SETUP.md)** 참고.

```
salesforce/        Apex(DataStreamModeFlipper) · CMDT · deploy.py · schedule.apex
rest-scripts/      API 검증용 REST 스크립트 (dc_datastream_swap.py · dc_full_refresh.py)
SETUP.md           수동 설정 가이드
```
