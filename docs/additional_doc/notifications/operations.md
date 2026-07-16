# Discord·Slack·Telegram 운영 알림

`app/notifications/service.py`는 호출자가 `title`, `body`, `severity`, `fields`만 넘기면
Discord/Slack/Telegram 채널 형식으로 변환해 전송하는 독립 인터페이스다.
현재 모의 결제 요청이 이 인터페이스를 사용한다.

```python
NotificationService().send(
    NotificationMessage(
        title="운영 승인 요청",
        body="관리자 확인이 필요합니다.",
        severity="notice",
        fields={"request_id": "...", "user": "닉네임"},
    )
)
```

```dotenv
NOTIFY_DISCORD_WEBHOOK_URL=
NOTIFY_SLACK_WEBHOOK_URL=
NOTIFY_TELEGRAM_BOT_TOKEN=
NOTIFY_TELEGRAM_CHAT_IDS=123456,-1001234567890
```

- 하나 이상의 채널이 성공하면 `delivered=true`다.
- 전부 미설정이면 사용자 모달에 `운영자의 알림 설정이 이뤄지지 않았습니다.`를 함께 표시한다.
- URL/토큰/원격 응답 원문은 API·감사 로그·DB 오류에 기록하지 않는다.
- 환경변수는 저장소 `.env`가 아니라 Secret Manager/Parameter Store/Vault 계열에서 주입한다.
- 운영에서는 HTTP 요청 thread에서 직접 보내기보다 outbox/queue worker로 분리하고 재시도·DLQ·채널별
  timeout/circuit breaker를 둔다. 현재 구현은 작은 단일 인스턴스용 동기 전송이다.
