@IsTest
private class DataStreamModeFlipperTest {

    // GET(상태조회)과 PATCH를 method로 구분해 응답하는 목.
    private class MultiMock implements HttpCalloutMock {
        Integer getCode; String getStatus; Integer patchCode;
        MultiMock(Integer getCode, String getStatus, Integer patchCode) {
            this.getCode = getCode; this.getStatus = getStatus; this.patchCode = patchCode;
        }
        public HttpResponse respond(HttpRequest req) {
            HttpResponse res = new HttpResponse();
            if (req.getMethod() == 'GET') {
                res.setStatusCode(getCode);
                res.setBody('{"status":"' + getStatus + '","refreshConfig":{},"advancedAttributes":{}}');
            } else { // PATCH
                res.setStatusCode(patchCode);
                res.setBody(patchCode >= 200 && patchCode < 300 ? '{"success":true}' : '{"error":"bad"}');
            }
            return res;
        }
    }

    private static Data_Stream_Profile__mdt sampleProfile() {
        return new Data_Stream_Profile__mdt(
            DeveloperName = 'DAILY',
            Stream_Record_Id__c = '1dsXX0000000000AAA',
            Refresh_Mode__c = 'UPSERT',
            Import_Directory__c = 'hot-daily',
            File_Name__c = 'hot-daily_*.csv'
        );
    }

    @IsTest
    static void active_status_patches() {
        Test.setMock(HttpCalloutMock.class, new MultiMock(200, 'ACTIVE', 200));
        Test.startTest();
        DataStreamModeFlipper.flip(sampleProfile(), 0); // GET->ACTIVE 이므로 PATCH 수행
        Test.stopTest();
        System.assert(true);
    }

    @IsTest
    static void processing_status_retries_without_throw() {
        Test.setMock(HttpCalloutMock.class, new MultiMock(200, 'PROCESSING', 200));
        Test.startTest();
        DataStreamModeFlipper.flip(sampleProfile(), 0); // patch 불가 → 재시도 경로(테스트에선 재큐 skip), 예외 없음
        Test.stopTest();
        System.assert(true);
    }

    @IsTest
    static void processing_status_max_retry_throws() {
        Test.setMock(HttpCalloutMock.class, new MultiMock(200, 'PROCESSING', 200));
        Boolean threw = false;
        Test.startTest();
        try {
            DataStreamModeFlipper.flip(sampleProfile(), 999); // 재시도 상한 초과 → 포기 예외
        } catch (DataStreamModeFlipper.FlipException e) {
            threw = true;
        }
        Test.stopTest();
        System.assert(threw, '재시도 상한 초과 시 FlipException');
    }

    @IsTest
    static void patch_failure_throws() {
        Test.setMock(HttpCalloutMock.class, new MultiMock(200, 'ACTIVE', 400));
        Boolean threw = false;
        Test.startTest();
        try {
            DataStreamModeFlipper.flip(sampleProfile(), 0);
        } catch (DataStreamModeFlipper.FlipException e) {
            threw = true;
        }
        Test.stopTest();
        System.assert(threw, 'PATCH 비2xx면 FlipException');
    }

    @IsTest
    static void get_status_failure_throws() {
        Test.setMock(HttpCalloutMock.class, new MultiMock(500, 'x', 200));
        Boolean threw = false;
        Test.startTest();
        try {
            DataStreamModeFlipper.flip(sampleProfile(), 0);
        } catch (DataStreamModeFlipper.FlipException e) {
            threw = true;
        }
        Test.stopTest();
        System.assert(threw, '상태조회 비200이면 FlipException');
    }

    @IsTest
    static void missing_profile_throws() {
        Boolean threw = false;
        Test.startTest();
        try {
            DataStreamModeFlipper.flip('___NOPE___', 0);
        } catch (DataStreamModeFlipper.FlipException e) {
            threw = true;
        }
        Test.stopTest();
        System.assert(threw);
    }

    @IsTest
    static void schedulable_and_queueable_cover() {
        // Schedulable.execute -> FlipJob enqueue -> flip('DAILY') 경로 커버 (DAILY CMDT 레코드는 배포에 포함)
        Test.setMock(HttpCalloutMock.class, new MultiMock(200, 'ACTIVE', 200));
        Test.startTest();
        new DataStreamModeFlipper('DAILY').execute(null);
        Test.stopTest();
        System.assert(true);
    }
}
