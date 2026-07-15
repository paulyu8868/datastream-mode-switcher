# 설정 가이드 (Manual Setup)

이 솔루션을 org에 올려 동작시키기까지 **사람이 직접 해야 하는 전 과정**입니다.
크게 세 부분: **A. Salesforce Setup(UI 클릭)** → **B. 배포** → **C. 스케줄 등록**.

> 표기: `<YOUR_MYDOMAIN>` 등 `<...>` 는 본인 org 값으로 치환. My Domain 은
> `https://<YOUR_MYDOMAIN>.my.salesforce.com` 형태이며, 개발/스크래치 org 은 보통
> `https://<name>.develop.my.salesforce.com` 입니다. **도메인이 틀리면 DNS 실패**하니 정확히.

## 사전 준비
- Data Cloud(Data 360) 활성 org
- **데이터 스트림 편집 권한**이 있는 유저 (아래 Run-As 유저로 쓸 것)
- 대상 데이터 스트림들의 `recordId`, 소스 디렉토리, 파일명 파악

---

## A. Salesforce Setup (UI에서 설정)

### A-1. Connected App (OAuth 클라이언트)
토큰을 발급하는 주체. 기존 앱 재사용 가능하나, **Client Credentials Flow 설정이 되어 있어야** 함.

1. Setup > **App Manager** > New Connected App (또는 기존 앱 편집)
2. **Enable OAuth Settings** 체크
   - Callback URL: `https://login.salesforce.com/services/oauth2/callback` (Client Credentials flow 에선 실제로 안 쓰이지만 필수 입력)
   - OAuth Scopes: `Manage user data via APIs (api)`, `Perform requests at any time (refresh_token, offline_access)`
3. 저장 후 **Manage > Edit Policies** :
   - **Client Credentials Flow 섹션 > Run As** 에 **데이터 스트림 편집 권한 유저** 지정 ← **필수**. 안 하면 토큰이 `no client credentials user enabled` 로 실패
4. **Manage Consumer Details** 에서 **Consumer Key / Consumer Secret** 확보 (A-2에서 사용)

### A-2. External Credential (시크릿 보관 + 토큰 발급 방식)
1. Setup > **Named Credentials** > **External Credentials** 탭 > New
   - Label/Name: 예 `DataCloud_Org_EC`
   - Authentication Protocol: **OAuth 2.0**
   - Authentication Flow Type: **`Client Credentials with Client Secret Flow`**
   - Identity Provider URL: `https://<YOUR_MYDOMAIN>.my.salesforce.com/services/oauth2/token`
   - **Scope: 공란으로 비워둘 것** ← Salesforce client_credentials 엔드포인트는 scope 파라미터를 거부(`scope parameter not supported`)
2. 저장 후 상세화면 > **Principals** 관련목록 > New  ← **여기가 client id/secret 넣는 곳**
   - Parameter Name: 자유 (예 `DataCloudPrincipal`), Sequence Number: 1
   - **Client ID / Client Secret**: A-1 의 Consumer Key / Secret (앞뒤 공백 없이 정확히)

### A-3. Named Credential (엔드포인트 + 자동 인증)
1. Setup > **Named Credentials** 탭 > New
   - **Name: `DataCloud_Org`** ← Apex 코드 상수(`DataStreamModeFlipper.cls` 의 `namedCredential`)와 **대소문자까지 정확히 일치**
   - URL: `https://<YOUR_MYDOMAIN>.my.salesforce.com` (base 도메인)
   - External Credential: A-2(`DataCloud_Org_EC`) 선택
   - Generate Authorization Header: 체크(기본)

### A-4. Permission Set (사용 인가)
Named Principal 이라도 "이 자격을 쓸 수 있는 유저"는 권한셋으로 열어줘야 함.

1. Setup > **Permission Sets** > New (예 `Data Stream Flipper Access`)
2. 그 권한셋 안 > **External Credential Principal Access** (Apps 섹션) > Edit > A-2 의 Principal 추가 > Save
3. 이 권한셋을 **스케줄을 등록/실행할 유저**(+ 검증할 본인)에게 할당

> ⚠️ 유저 두 명이 관여: **① Apex 실행 유저**(스케줄 등록자, 위 권한셋 필요) / **② Client Credentials Run-As 유저**(A-1, 데이터 스트림 편집 권한 필요). 같은 유저면 가장 단순.

---

## B. 배포

```bash
cp .env.example .env      # SF_LOGIN_URL, SF_CLIENT_ID, SF_CLIENT_SECRET 채우기
cd salesforce
python scripts/deploy.py            # checkOnly 검증
python scripts/deploy.py --deploy   # 실제 배포 (오브젝트+클래스 → 레코드, 2단계)
# sf CLI 가 있으면: sf project deploy start -d force-app -l RunLocalTests
```

배포 후 **CMDT 레코드 값**을 실제 스트림 값으로 채우기 —
`salesforce/force-app/main/default/customMetadata/Data_Stream_Profile.{DAILY,MONTHLY}.md-meta.xml`
의 `Stream_Record_Id__c` / `Import_Directory__c` / `File_Name__c` / `Refresh_Mode__c`.
(Setup > Custom Metadata Types > Data Stream Profile 에서 편집해도 됨 — 재배포 불필요)

---

## C. 스케줄 등록

`salesforce/scripts/schedule.apex` 를 **원하는 타임존 유저로** Execute Anonymous.
cron 은 **등록 유저의 타임존 기준** — KST 원하면 타임존이 `Asia/Seoul` 인 유저로 실행.

```apex
System.schedule('DataStream flip -> MONTHLY', '0 15 0 * * ?', new DataStreamModeFlipper('MONTHLY')); // 00:15
System.schedule('DataStream flip -> DAILY',   '0 0 3 * * ?',  new DataStreamModeFlipper('DAILY'));   // 03:00
```

확인: Setup > Scheduled Jobs, 또는
`SELECT CronJobDetail.Name, State, NextFireTime FROM CronTrigger`.

---

## 검증

Execute Anonymous 로 즉시 한 번 돌려보기 (읽고 바꾸는지 확인 후 원복):
```apex
System.enqueueJob(new DataStreamModeFlipper.FlipJob('MONTHLY'));
// → 스트림 config 가 monthly 로 바뀌는지 확인 후
System.enqueueJob(new DataStreamModeFlipper.FlipJob('DAILY'));   // 원복
```

---

## 트러블슈팅 (실제로 겪은 것들)

| 증상 | 원인 / 해결 |
|------|-------------|
| `no client credentials user enabled` | A-1 에서 **Client Credentials Flow 의 Run As 유저 미지정** → 지정 |
| `Unable to fetch the OAuth token` + DNS/`ERR_DNS_FAIL` | URL 도메인 오류. **A-2 토큰 URL 과 A-3 base URL 둘 다** 정확한 My Domain(dev면 `.develop` 포함)인지 |
| `scope parameter not supported` | A-2 의 **Scope 를 공란**으로 |
| `invalid_client / invalid client credentials` | A-2 Principal 의 client id/secret 오타 또는 **다른 앱 값** → 정확히 재입력 |
| `no access to external credential` | A-4 **권한셋을 실행 유저에게 미할당** |
| `Streams can only be patched when in Active, Error, or Inactive status` | 스트림이 처리중. **코드가 5분 뒤 자동 재시도**(최대 10회)하므로 대개 무시 가능 |
