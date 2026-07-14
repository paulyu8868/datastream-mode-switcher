# Data Stream Mode Flipper — 배포/설정 가이드

데이터 스트림 하나의 config(refreshMode/디렉토리/파일명)를 하루 2번 자동 전환하는 org-native 솔루션.

- **00:15** → MONTHLY 프로파일 (TOTAL_REPLACE, monthly 폴더) — monthly 파일(00:30) 수신 대비
- **03:00** → DAILY 프로파일 (UPSERT, daily 폴더) 복귀
- 그 사이 데이터 스트림의 **native 30분 스케줄**이 현재 config대로 적재.

## 구성 요소
| 파일 | 역할 |
|------|------|
| `classes/DataStreamModeFlipper.cls` | Schedulable → Queueable → `ssot/data-streams/{id}` PATCH 콜아웃 |
| `classes/DataStreamModeFlipperTest.cls` | 테스트(콜아웃 목) |
| `objects/Data_Stream_Profile__mdt/` | 프로파일 설정 저장용 커스텀 메타데이터 타입 |
| `customMetadata/Data_Stream_Profile.DAILY/MONTHLY` | DAILY/MONTHLY 값 (streamId·mode·dir·file) |
| `scripts/schedule.apex` | 스케줄 등록용 Anonymous Apex |

---

## 1) 프로파일 값부터 실제 값으로 수정
`customMetadata/Data_Stream_Profile.DAILY.md-meta.xml`, `...MONTHLY.md-meta.xml` 의
`Stream_Record_Id__c` / `Import_Directory__c` / `File_Name__c` 를 **실제 운영 스트림 값**으로 교체.
(현재는 테스트에 썼던 `REPLACE_WITH_STREAM_RECORD_ID` / `hot-*` 예시값)

## 2) Named Credential 만들기  ← 유일하게 남은 수동 단계 (브라우저 승인 없음)
우리 org는 이미 **Client Credentials flow**로 data-stream API가 되므로(이 세션에서 검증됨),
Named Credential도 **Client Credentials 방식**으로 잡는다. → OAuth 브라우저 consent 불필요, `.env`의 Connected App 재활용.

**A. External Credential** (Setup > Named Credentials > **External Credentials** 탭 > New)
- Label/Name: 예 `DataCloud_Org_EC`
- Authentication Protocol: **OAuth 2.0**
- Authentication Flow Type: **`Client Credentials with Client Secret Flow`**  ← 이걸 골라야 뒤에서 client id/secret 필드가 나옴
- Identity Provider URL(토큰 엔드포인트): `https://YOUR_MYDOMAIN.my.salesforce.com/services/oauth2/token`  ← **`.develop` 반드시 포함** (없으면 호스트 자체가 존재 안 함)
- **Scope: 반드시 공란** ← Salesforce 자체 client_credentials 엔드포인트는 scope 파라미터를 거부함("scope parameter not supported")
- 저장

> ⚠️ 선행조건: `.env`의 Connected App(**YOUR_CONNECTED_APP**)에서 **Client Credentials Flow 활성화 + Run As 유저 지정**이 되어 있어야 함. 안 되어 있으면 토큰이 "no client credentials user enabled"로 실패. (App Manager > YOUR_CONNECTED_APP > Manage > Edit Policies)

**A-2. Principal 추가** (위 External Credential 상세화면 > **Principals** 관련목록 > New)  ← **여기가 client id/secret 넣는 곳**
- Parameter Name: 예 `DataCloudPrincipal`, Sequence Number: 1
- **Client ID / Client Secret**: `.env`의 `SF_CLIENT_ID` / `SF_CLIENT_SECRET` (기존 Connected App)
- (토큰 400 나면) **"Pass client credentials in request body"** 옵션 ON
- ※ **Client Secret 입력이 제가 못 하는 유일한 부분** — 시크릿은 메타데이터로 배포 안 됨.

**B. Named Credential** (Named Credentials 탭 > New) — 이름: **`DataCloud_Org`** (대문자 O)
- ⚠️ Name(API 이름)이 코드의 `DataStreamModeFlipper.cls` > `namedCredential = 'DataCloud_Org'` 와 **대소문자까지 정확히** 일치해야 함.
- URL: `https://YOUR_MYDOMAIN.my.salesforce.com`  (코어 도메인. 관리 API는 코어 토큰+코어 도메인 → DC 토큰 교환 불필요)
- External Credential: 위 A 선택 / "Generate Authorization Header" 체크

**C. 권한셋으로 Principal 접근 허용**  ← External Credential 화면 말고 **Permission Set 쪽에서**
1. Setup > **Permission Sets** > New (예: `Data Stream Flipper Access`)
2. 그 권한셋 안 > **`External Credential Principal Access`** (Apps 섹션) > Edit > 위 A-2의 Principal 추가 > Save
3. 이 권한셋을 **스케줄 등록/실행 유저**(+ 테스트할 본인)에게 할당

> ⚠️ 콜아웃은 client_credentials의 **Run-As 유저** 권한으로 실행됨. 그 유저에게 **Data Cloud 데이터 스트림 편집 권한** 필요. (이 세션에서 이미 이 Connected App으로 PATCH 성공 → 권한 확인됨.)

## 3) 배포  ✅ 완료됨
Apex 클래스 + CMDT 타입 + DAILY/MONTHLY 레코드는 **이미 배포됨** (sf CLI 없이 `scripts/deploy.py`로 Metadata API 배포).
값만 바꿔 다시 배포하려면:
```bash
python scripts/deploy.py            # Phase1 checkOnly 검증 (변경 없음)
python scripts/deploy.py --deploy   # 실제 배포 (Phase1 오브젝트+클래스 → Phase2 레코드)
```
> 배포 메모: ① CustomMetadata 레코드는 CMDT 타입과 **같은 배포에 못 넣음**(UNKNOWN_EXCEPTION) → 2단계 분리. ② raw MDAPI는 레코드 XML에 `xmlns:xsd` 선언 필요.
>
> sf CLI가 있는 환경이면 대신: `sf project deploy start -d force-app -o myorg -l RunLocalTests`

## 4) 스케줄 등록 (1회)
`scripts/schedule.apex` 내용을 **KST 타임존 사용자로** Execute Anonymous 실행:
```bash
sf apex run -f scripts/schedule.apex -o myorg
```
확인:
```bash
sf data query -q "SELECT CronJobDetail.Name, NextFireTime, State FROM CronTrigger" -o myorg
```

## 5) 수동 검증 (선택)
Execute Anonymous 로 즉시 한 번 flip 돌려보기:
```apex
System.enqueueJob(new DataStreamModeFlipper.FlipJob('MONTHLY'));
```
→ 데이터 스트림 config가 monthly로 바뀌는지 UI/GET으로 확인 후 `'DAILY'`로 원복.

---

## 값만 바꾸고 싶을 때
스트림 ID·디렉토리·파일명·모드는 전부 **Custom Metadata**에 있으므로, 코드 재배포 없이
`customMetadata/*.md-meta.xml` 만 고쳐서 재배포(또는 Setup > Custom Metadata Types 에서 레코드 편집).

## 주의점 요약
- **타임존**: 스케줄은 등록 사용자 TZ 기준. 00:15/03:00 KST 원하면 KST 유저로 등록.
- **통합 유저 권한**: 데이터 스트림 편집 권한 필수.
- **monthly 업로드(00:30) < flip-to-daily(03:00)**: 워터마크상 monthly 파일이 daily 마지막 run 이후에 있어야 인식됨(현재 타이밍 OK).
- **실패 대비**: 현재는 예외 던지고 로그(System.debug). 운영에선 실패 시 Platform Event/커스텀 로그로 알림 추가 권장.
