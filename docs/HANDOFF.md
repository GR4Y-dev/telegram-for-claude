# Text chat and voice-note handoff

## Scope

The Telegram assistant previously handled calls, `/call`, `/text`, `/update`, and outbox
notifications. Ordinary messages were ignored. This change adds private owner text chat,
typing indicators, sequential conversation memory, `/new`, voice-note transcripts and text
answers, and fresh configured-zone/UTC time in each Claude invocation, including calls.
The WhatsApp repository needs no changes for these Telegram assistant features.

## Implementation

- `telegram-caller/caller.py`: shared Claude runner with separate phone/chat prompts; chat
  state belongs to `Assistant`, call state still belongs to `Call`. Chat memory resets when
  the service restarts. `/new` resets the session reference, not stored history files.
- Chat turns and resets are serialized, with at most ten pending turns. Commands continue
  using their explicit handlers. Only the owner in a private chat is accepted.
- Voice notes use the existing transcription endpoint/key, with a 5-minute/20-MB limit,
  download timeout, transcript confirmation, and text answers. No voice synthesis for replies.
- Long replies are sent without Markdown parsing in Unicode-safe chunks. Provider failures
  produce a helpful response without exposing raw provider error text to the chat.
- `tests/test_chat.py`: deterministic offline tests using mocked Telegram/Claude/STT.
  Included in GitHub Actions alongside the existing connector, updater and call tests.

## Verification and rollout

Run `python -m unittest discover -s tests -p test_chat.py -v`, then the existing tests in
`.github/workflows/tests.yml`. Call tests require ffmpeg and Microsoft's online voices.
Real Claude/Telegram/STT behavior still needs checking on the owner's server.

Local verification: 13 chat unit tests, 54 connector checks, 31 call-assistant checks
(real speech synthesis, mocked Telegram, real Claude checks skipped), and 8 updater checks
passed. GitHub Actions should confirm the Linux environment before merging.

After review and passing GitHub checks, merge to `main`. The existing nightly updater can
then deploy it, or the owner can send `/update`. No connector reconnection is required:
the connector tool list has not changed. Verify the existing `TIMEZONE` setting is correct.

Live smoke checks: ask for local and UTC time; give a name and ask it back; send `/new` and
check the old name is not remembered; send a short voice note and check its transcript and
answer; make a call and ask the time. Verify `/text` and `/update` still work. A Claude
subscription limit on the server still prevents model replies until access is restored.

Do not put account sessions, keys, connector URLs, or `.env` contents into Git or chats.
