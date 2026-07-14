/**
 * 데이터 스트림 config를 프로파일(DAILY/MONTHLY)에 맞게 flip 하는 Scheduled Apex.
 *
 * 흐름: System.schedule 로 하루 2번 실행 → Queueable로 넘겨 콜아웃 →
 *       GET으로 스트림 상태 확인 → patch 가능 상태면 ssot/data-streams/{id} PATCH.
 *
 * 하드닝: 스트림이 처리중(ACTIVE/Error/Inactive가 아님)이면 PATCH가 400으로 거부됨.
 *        → 그 경우 5분 뒤 재시도(최대 MAX_RETRIES회)로 재큐(AsyncOptions 지연 큐).
 *
 * 인증: Named Credential 'DataCloud_Org' 가 OAuth 토큰 자동 주입 (코드에 토큰/시크릿 없음).
 * 설정값: Data_Stream_Profile__mdt 커스텀 메타데이터에서 읽음 (재배포 없이 값 변경).
 */
public with sharing class DataStreamModeFlipper implements Schedulable {

    @TestVisible
    private static String namedCredential = 'DataCloud_Org';
    private static final String API_VERSION = 'v66.0';

    // PATCH 가능한 스트림 상태 (그 외 PROCESSING 등이면 재시도)
    private static final Set<String> PATCHABLE = new Set<String>{ 'ACTIVE', 'Error', 'Inactive' };
    private static final Integer MAX_RETRIES = 10;       // 재시도 횟수 상한
    private static final Integer RETRY_DELAY_MIN = 5;    // 재시도 간격(분, 0~10)

    private final String profileName;

    public DataStreamModeFlipper(String profileName) {
        this.profileName = profileName;
    }

    public void execute(SchedulableContext sc) {
        System.enqueueJob(new FlipJob(profileName, 0));
    }

    public class FlipJob implements Queueable, Database.AllowsCallouts {
        private final String profileName;
        private final Integer attempt;
        public FlipJob(String profileName) { this(profileName, 0); }
        public FlipJob(String profileName, Integer attempt) {
            this.profileName = profileName;
            this.attempt = attempt;
        }
        public void execute(QueueableContext qc) {
            DataStreamModeFlipper.flip(profileName, attempt);
        }
    }

    @TestVisible
    private static void flip(String profileName, Integer attempt) {
        Data_Stream_Profile__mdt p = Data_Stream_Profile__mdt.getInstance(profileName);
        if (p == null) {
            throw new FlipException('Data_Stream_Profile__mdt 레코드 없음: ' + profileName);
        }
        flip(p, attempt);
    }

    @TestVisible
    private static void flip(Data_Stream_Profile__mdt p, Integer attempt) {
        String status = getStatus(p.Stream_Record_Id__c);

        if (!PATCHABLE.contains(status)) {
            // 처리중 → 지연 재시도
            if (attempt >= MAX_RETRIES) {
                throw new FlipException('Flip 포기 (' + p.DeveloperName + '): status=' + status
                    + ' 가 ' + MAX_RETRIES + '회 재시도 후에도 patch 불가');
            }
            if (!Test.isRunningTest()) {
                AsyncOptions ao = new AsyncOptions();
                ao.MinimumQueueableDelayInMinutes = RETRY_DELAY_MIN;
                System.enqueueJob(new FlipJob(p.DeveloperName, attempt + 1), ao);
            }
            System.debug(LoggingLevel.INFO, 'Flip 지연 재시도 예약 (' + p.DeveloperName
                + ') status=' + status + ' attempt=' + (attempt + 1));
            return;
        }

        doPatch(p);
    }

    // 스트림 현재 status 조회
    @TestVisible
    private static String getStatus(String streamId) {
        HttpRequest req = new HttpRequest();
        req.setEndpoint('callout:' + namedCredential + '/services/data/' + API_VERSION
            + '/ssot/data-streams/' + streamId);
        req.setMethod('GET');
        req.setTimeout(120000);
        HttpResponse res = new Http().send(req);
        if (res.getStatusCode() != 200) {
            throw new FlipException('상태 조회 실패 (' + streamId + '): '
                + res.getStatusCode() + ' ' + res.getBody());
        }
        Map<String, Object> body = (Map<String, Object>) JSON.deserializeUntyped(res.getBody());
        return (String) body.get('status');
    }

    // 실제 PATCH 콜아웃
    @TestVisible
    private static HttpResponse doPatch(Data_Stream_Profile__mdt p) {
        Map<String, Object> body = new Map<String, Object>{
            'refreshConfig' => new Map<String, Object>{ 'refreshMode' => p.Refresh_Mode__c },
            'advancedAttributes' => new Map<String, Object>{
                'importDirectory' => p.Import_Directory__c,
                'fileName' => p.File_Name__c
            }
        };
        HttpRequest req = new HttpRequest();
        req.setEndpoint('callout:' + namedCredential + '/services/data/' + API_VERSION
            + '/ssot/data-streams/' + p.Stream_Record_Id__c);
        req.setMethod('PATCH');
        req.setHeader('Content-Type', 'application/json');
        req.setBody(JSON.serialize(body));
        req.setTimeout(120000);
        HttpResponse res = new Http().send(req);
        Integer code = res.getStatusCode();
        if (code < 200 || code >= 300) {
            throw new FlipException('Flip 실패 (' + p.DeveloperName + '): ' + code + ' ' + res.getBody());
        }
        System.debug(LoggingLevel.INFO, 'Flip OK (' + p.DeveloperName + ') mode='
            + p.Refresh_Mode__c + ' dir=' + p.Import_Directory__c + ' -> ' + code);
        return res;
    }

    public class FlipException extends Exception {}
}
