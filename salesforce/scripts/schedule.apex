// Execute Anonymous 로 1회 실행하여 두 개의 스케줄을 등록.
// ⚠️ cron 시각은 "이 코드를 실행하는 사용자의 타임존" 기준. KST 사용자로 실행할 것.
// cron 포맷: Seconds Minutes Hours Day_of_month Month Day_of_week [Year]

// daily 종료 후 monthly 프로파일로 (00:15) — monthly 파일(00:30)이 오기 직전
System.schedule('DataStream flip -> MONTHLY', '0 15 0 * * ?', new DataStreamModeFlipper('MONTHLY'));

// monthly 처리 끝난 뒤 daily 프로파일로 복귀 (03:00)
System.schedule('DataStream flip -> DAILY', '0 0 3 * * ?', new DataStreamModeFlipper('DAILY'));

// 확인: System.debug([SELECT CronJobDetail.Name, NextFireTime, State FROM CronTrigger]);
// 해제: 예약 화면(Setup > Scheduled Jobs)에서 삭제하거나 System.abortJob('<jobId>');
