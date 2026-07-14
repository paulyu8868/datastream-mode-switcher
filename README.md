# Data Cloud Data Stream Mode Scheduler

Salesforce **Data Cloud (Data 360)** 데이터 스트림 하나의 설정(refresh mode · 소스 디렉토리 · 파일명)을 **하루 2번 자동으로 전환**해서, 한 스트림으로 **daily 증분(UPSERT)** 과 **monthly 전체 교체(TOTAL_REPLACE)** 를 모두 처리하는 org-native 솔루션.

전부 **Salesforce 안(Scheduled Apex + Named Credential)** 에서 돕니다. 외부 서버·Lambda·cron 불필요, 코드에 시크릿 없음.

---

## 문제

데이터 스트림 1개는 **(소스 디렉토리 · 파일명 · refresh mode · 스케줄) 조합을 하나만** 가질 수 있습니다. 그런데 실제 적재 요구는 두 가지였습니다:

| | 디렉토리 | 파일명 | 모드 | 업로드 |
|---|---|---|---|---|
| **daily** | `hot-daily` | `hot-daily_*.csv` | `UPSERT` (증분) | 08:00~22:00, 30분 간격 |
| **monthly** | `hot-monthly` | `hot-monthly_*.csv` | `TOTAL_REPLACE` (전체 교체) | 매일 00:30, 마스터 스냅샷 |

→ **native 스케줄 하나로는 둘을 동시에 담을 수 없습니다.**

### 게다가: 워터마크 제약
데이터 스트림은 **"마지막 run 처리 일시"를 워터마크로 삼아 그 이후 파일만 가져옵니다.** 이 워터마크는 **refresh mode 와 무관하게 스트림당 하나로 공유**됩니다. 그래서 run 단위로 config를 바꿔치기(swap)하면 워터마크가 오염돼 daily 파일이 누락됩니다. → **run 단위 swap은 불가.**

---

## 해결

스트림의 **native 30분 스케줄은 그대로 계속 돌게 두고**, 스트림의 **config만 하루 2번 flip** 합니다 (각 config를 몇 시간씩 유지 → 워터마크/비동기 문제 회피).

```
평소 (08:00~22:00)  : native 30분 스케줄이 daily 파일 UPSERT 적재
00:15 KST           : Apex 가 config 를 MONTHLY 로 flip (TOTAL_REPLACE / hot-monthly)
00:30               : monthly 마스터 파일 도착 → native 스케줄이 full refresh (전체 교체)
03:00 KST           : Apex 가 config 를 DAILY 로 flip 복귀 (스트림 처리중이면 자동 재시도)
```

- monthly 업로드(00:30)가 daily 마지막 run(~22:00) **이후**라 워터마크에 정상 포착됨.
- monthly TOTAL_REPLACE 가 매일 daily 데이터를 마스터로 덮어씀(의도된 설계) → 낮 동안의 소소한 누락도 **자가 치유**.

### 하루 사이클

| 시각(KST) | config | 동작 |
|---|---|---|
| 08:00~22:00 | DAILY (UPSERT) | 30분마다 daily 파일 증분 적재 |
| **00:15** | → MONTHLY flip | 이후 monthly 폴더 바라봄 (파일 오기 전엔 no-op) |
| 00:30 | MONTHLY | monthly 파일 도착 → 다음 run 이 전체 교체 |
| **03:00** | → DAILY flip | daily 로 복귀 (처리중이면 5분 뒤 재시도) |

---

## 아키텍처

```
Scheduled Apex (하루 2번)            Named Credential
  00:15 → FlipJob('MONTHLY')  ──▶  callout:DataCloud_Org  ──▶  PATCH /ssot/data-streams/{id}
  03:00 → FlipJob('DAILY')          (OAuth 토큰 자동 주입)         { refreshConfig, advancedAttributes }
      │
      └─ 값은 Data_Stream_Profile__mdt (Custom Metadata) 에서 읽음 → 재배포 없이 변경
```

**FlipJob 동작**: `GET`으로 스트림 status 확인 → `ACTIVE/Error/Inactive` 면 `PATCH`, 아니면(처리중) **5분 뒤 재시도(최대 10회, `AsyncOptions` 지연 큐)**. 데이터 스트림은 처리중일 때 PATCH가 400으로 거부되기 때문.

---

## 레포 구조

```
salesforce/                         # sf CLI 없이도 배포 가능한 SFDX 소스
  force-app/main/default/
    classes/DataStreamModeFlipper.cls        # Schedulable → Queueable → PATCH (+재시도 하드닝)
    classes/DataStreamModeFlipperTest.cls     # 콜아웃 목 테스트
    objects/Data_Stream_Profile__mdt/         # 프로파일 설정용 Custom Metadata Type
    customMetadata/Data_Stream_Profile.{DAILY,MONTHLY}.md-meta.xml   # 프로파일 값
  scripts/deploy.py                 # sf CLI 없이 Metadata API(SOAP) 로 배포 (2-phase)
  scripts/schedule.apex             # 스케줄 2개 등록 (Execute Anonymous)
  SETUP.md                          # Named Credential · 배포 · 스케줄 상세 가이드
rest-scripts/                       # API 검증에 쓴 REST 스크립트 (Apex 솔루션의 전신)
  dc_datastream_swap.py             # GET→UPDATE(dir/file/mode)→RUN→원복 1회성 스왑
  dc_full_refresh.py                # refreshMode 만 TOTAL_REPLACE→run→원복
.env.example
```

---

## 빠른 시작

> 자세한 절차(특히 Named Credential)는 **[salesforce/SETUP.md](salesforce/SETUP.md)** 참고.

1. **프로파일 값 채우기** — `customMetadata/Data_Stream_Profile.{DAILY,MONTHLY}.md-meta.xml` 의
   `Stream_Record_Id__c` / `Import_Directory__c` / `File_Name__c` / `Refresh_Mode__c` 를 실제 스트림 값으로.
2. **Named Credential `DataCloud_Org`** 생성 (Client Credentials flow, 시크릿은 코드 아닌 External Credential 에). → SETUP.md 2번.
3. **배포**
   ```bash
   cp .env.example .env        # 값 채우기 (SF_LOGIN_URL, SF_CLIENT_ID, SF_CLIENT_SECRET ...)
   cd salesforce
   python scripts/deploy.py            # checkOnly 검증
   python scripts/deploy.py --deploy   # 실제 배포
   # sf CLI 가 있으면: sf project deploy start -d force-app -l RunLocalTests
   ```
4. **스케줄 등록** — `salesforce/scripts/schedule.apex` 를 **원하는 타임존 유저**로 Execute Anonymous
   (cron 은 등록 유저 타임존 기준. KST 원하면 KST 유저로 실행).

---

## 설정값 바꾸기 (재배포 없이)
스트림 ID · 디렉토리 · 파일명 · 모드는 전부 **Custom Metadata** 에 있습니다.
Setup > Custom Metadata Types > Data Stream Profile > 레코드 편집, 또는 `.md-meta.xml` 수정 후 재배포.

---

## REST 스크립트 (rest-scripts/)
Apex 솔루션을 만들기 전, ssot/data-streams API 동작을 검증한 도구들. 단발성 운영/디버깅에도 유용.

- **`dc_datastream_swap.py`** — 대상 스트림을 GET 으로 스냅샷 → 디렉토리/파일명/모드를 목표값으로 UPDATE → RUN 트리거 → 완료 대기 후 원복.
  ```bash
  python rest-scripts/dc_datastream_swap.py --stream-id <ID> --dir hot-monthly --orig-dir hot-daily --file "hot-monthly_*.csv" --mode TOTAL_REPLACE
  ```
- **`dc_full_refresh.py`** — refreshMode 를 TOTAL_REPLACE 로 바꿔 1회 full refresh 실행 후 UPSERT 로 원복.

---

## 배운 점 / 함정 (실전 노트)

Data Cloud data stream API 를 다루며 확인한 것들:

- **run 은 비동기.** `POST .../actions/run` 이 `201` 을 줘도 **적재 완료가 아님.** config 를 되돌리기 전 `lastRefreshDate` 가 바뀔 때까지 기다려야 함 (안 그러면 원복된 설정으로 잡이 돌아 full refresh 가 안 먹음).
- **PATCH 는 스트림이 `ACTIVE`/`Error`/`Inactive` 일 때만** 가능. 처리중이면 `400 "Streams can only be patched when in Active, Error, or Inactive status"` → 재시도 필요.
- **파일 선택 워터마크**는 마지막 run 일시 기준이고 mode 무관·스트림당 공유 → run 단위 swap 설계 불가.
- **`importDirectory` 는 GET 응답에 스트림마다 있을 수도/없을 수도** 있음 → 원복 시 원본 디렉토리를 명시적으로 넘기는 게 안전.
- **Named Credential (Client Credentials)**: 토큰 URL 과 base URL 둘 다 정확한 My Domain 필요(도메인 틀리면 DNS 실패). **Scope 는 공란**(Salesforce client_credentials 엔드포인트는 scope 파라미터 거부). Connected App 에 **Client Credentials Flow + Run As 유저** 필수.
- **sf CLI 없이 Metadata API(SOAP) 배포** 시: CustomMetadata **레코드는 타입과 별도 배포**(같이 넣으면 `UNKNOWN_EXCEPTION`), 레코드 XML 에 `xmlns:xsd` 선언 필요.

---

## 요구사항
- Salesforce org (Data Cloud / Data 360 활성화), 데이터 스트림 편집 권한
- Python 3.9+ (`requests`, `python-dotenv`, JWT 쓸 경우 `pyjwt`) — 배포/REST 스크립트용
- (선택) Salesforce CLI `sf` — 있으면 배포에 사용 가능
