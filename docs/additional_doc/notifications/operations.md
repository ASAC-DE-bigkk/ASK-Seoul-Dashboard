# Discord·Slack·Telegram 운영 알림

`app/notifications/service.py`는 호출자가 `title`, `body`, `severity`, `fields`만 넘기면
Discord/Slack/Telegram 채널 형식으로 변환해 전송하는 독립 인터페이스다. 현재 모의 결제 요청이
이 인터페이스와 DB outbox를 사용한다.

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
- Slack·Discord는 HTTPS 공식 webhook host/path만 허용하고 redirect를 따르지 않는다. Telegram bot
  token과 chat ID도 형식을 검증한다.
- URL/토큰/원격 응답 원문은 API·감사 로그·DB 오류에 기록하지 않고 대상은 마스킹한다.
- Discord mention은 비활성화하며 메시지·필드 길이는 각 채널 한도에 맞게 제한한다.
- 환경변수는 저장소 `.env`가 아니라 Secret Manager/Parameter Store/Vault 계열에서 주입한다.

## outbox 처리

결제 요청 transaction에서 채널별 `queued` 또는 `not_configured` delivery를 먼저 저장한 뒤 commit
이후 background task가 전송한다. worker는 `queued → processing`을 원자적으로 claim하므로 여러
프로세스가 동시에 같은 delivery를 점유하지 않는다. 다만 외부 전송 성공 직후 DB에 `delivered`를
기록하기 전에 프로세스가 중단되면 stale 복구 후 같은 메시지가 재전송될 수 있으므로 전달 의미는
at-least-once다. 수신 채널에서 중복 알림을 허용하고, 메시지의 요청 ID로 동일 요청임을 식별한다.

| 상태 | 의미 |
|---|---|
| `queued` | 전송 대기 또는 재시도 대기 |
| `processing` | worker가 원자적으로 점유 |
| `delivered` | 원격 채널이 성공 응답 |
| `failed` | 설정은 있으나 전송 실패 |
| `not_configured` | 채널 설정 없음 |

웹 프로세스 중단 직후 남은 요청을 복구하려면 아래 명령을 cron, systemd timer 또는 배포 환경의
scheduled job으로 주기 실행한다.

```bash
.venv/bin/python scripts/process_notifications.py --limit 100 --stale-minutes 5
```

이 명령은 오래된 `processing`을 먼저 `queued`로 되돌리고 대기 건을 처리한다. 운영 콘솔에서는
실패한 결제 알림을 다시 queue에 넣을 수 있다. 원격 서비스 장애가 길어지는 환경에서는 현재의
수동/주기 재시도 위에 exponential backoff와 최대 시도 횟수, DLQ를 추가한다.
