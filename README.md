# Data Stream Mode Switcher

Salesforce **Data Cloud(Data 360)** 데이터 스트림 하나의 설정(refresh mode · 소스 디렉토리 · 파일명)을 **하루 2번 자동 전환**하는 Scheduled Apex. 한 스트림으로 **daily 증분(UPSERT)** 과 **monthly 전체 교체(TOTAL_REPLACE)** 를 모두 처리한다. 외부 인프라 없이 org 안(Scheduled Apex + Named Credential)에서 동작하며 코드에 시크릿이 없다.

## Apex 클래스

**`DataStreamModeFlipper`** — 프로파일(`DAILY`/`MONTHLY`)을 받아 대상 데이터 스트림의 config 를 전환한다.

### 구성 (클래스 / 메서드)

| 요소 | 타입 | 역할 |
|------|------|------|
| `DataStreamModeFlipper` | `Schedulable` | 스케줄 진입점 |
| ├ `execute(SchedulableContext)` | | `FlipJob` 을 큐에 넣음(콜아웃을 async 로 분리) |
| `FlipJob` | `Queueable, Database.AllowsCallouts` | 콜아웃 실행 단위 (재시도 시 attempt 카운터 보유) |
| ├ `execute(QueueableContext)` | | `flip()` 호출 |
| `flip(profileName, attempt)` | `static` | CMDT 조회 + 상태 확인 + 분기(전환/재시도) 오케스트레이션 |
| `getStatus(streamId)` | `static` | 스트림 status 조회 (**GET** 콜아웃) |
| `doPatch(profile)` | `static` | config 전환 (**PATCH** 콜아웃) |
| `Data_Stream_Profile__mdt` | Custom Metadata | `DAILY`/`MONTHLY` 별 `Stream_Record_Id__c`·`Refresh_Mode__c`·`Import_Directory__c`·`File_Name__c` 저장 |

### 실행 순서

```
스케줄 발화(00:15 / 03:00)
  └─ DataStreamModeFlipper.execute()          # ① 스케줄 진입
       └─ System.enqueueJob(FlipJob(profile, 0))   # ② 콜아웃을 Queueable 로 분리
            └─ FlipJob.execute() → flip(profile, 0)
```

1. **① 스케줄 진입** — `System.schedule` 로 등록된 잡이 발화, `execute()` 실행.
2. **② 큐 분리** — 곧바로 `FlipJob` 을 enqueue. (Scheduled 컨텍스트의 콜아웃 제약 회피 + 재시도 체이닝 목적)
3. **③ 프로파일 로드** — `Data_Stream_Profile__mdt.getInstance(profileName)` 로 대상 `streamId` · mode · dir · file 획득. (없으면 `FlipException`)
4. **④ 상태 확인 (GET)** — `getStatus()` 가 스트림을 조회해 응답의 `status` 를 읽음.

   ```
   GET  callout:DataCloud_Org/services/data/v66.0/ssot/data-streams/{streamId}
   →    { "status": "ACTIVE", ... }
   ```
5. **⑤ 분기**
   - `status` 가 `ACTIVE` / `Error` / `Inactive` → **⑥ 전환**
   - 그 외(처리중 등) → **⑦ 재시도** (처리중 스트림은 PATCH 가 400 으로 거부되기 때문)
6. **⑥ 전환 (PATCH)** — `doPatch()` 가 config 를 프로파일 값으로 변경. 2xx 아니면 `FlipException`.

   ```
   PATCH  callout:DataCloud_Org/services/data/v66.0/ssot/data-streams/{streamId}
   body   {
            "refreshConfig":     { "refreshMode": "TOTAL_REPLACE" },
            "advancedAttributes":{ "importDirectory": "hot-monthly",
                                   "fileName": "hot-monthly_*.csv" }
          }
   ```
7. **⑦ 재시도** — `attempt < 10` 이면 `AsyncOptions.MinimumQueueableDelayInMinutes = 5` 로 `FlipJob(profile, attempt+1)` 재큐(5분 뒤). `attempt ≥ 10` 이면 포기하며 `FlipException`.

### 호출하는 API (2개, 모두 Named Credential 경유)

| 순서 | 메서드 · 엔드포인트 | 목적 |
|------|---------------------|------|
| ④ | `GET  /services/data/v66.0/ssot/data-streams/{id}` | 현재 `status` 조회 (전환 가능 여부 판단) |
| ⑥ | `PATCH /services/data/v66.0/ssot/data-streams/{id}` | `refreshMode` · `importDirectory` · `fileName` 전환 |

- 두 호출 모두 `callout:DataCloud_Org` (Named Credential) 로 나가며, **OAuth 토큰 발급·주입은 플랫폼이 처리**(코드에 시크릿/토큰 없음).
- 전환 값은 **`Data_Stream_Profile__mdt`** 에서 읽으므로 **코드 재배포 없이** 스트림 ID·디렉토리·파일명·모드를 바꿀 수 있음.

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
