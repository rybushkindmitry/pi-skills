# pi-skills

Личная коллекция скиллов для [Pi coding agent](https://pi.dev).

## Установка

```bash
pi install git:github.com/rybushkindmitry/pi-skills@v1
```

Пакет подключается в `~/.pi/agent/settings.json` (`packages`) и применяется
во всех проектах. Обновление: `pi update --extensions`.

## Скиллы

| Скилл | Назначение |
| --- | --- |
| [`structuring-repo-documentation`](skills/structuring-repo-documentation/SKILL.md) | Документационная культура репо: ADR (контекст/альтернативы/последствия, supersedes), split факты/планы (architecture-current/target), implementation-status с датированными свидетельствами; README остаётся тонким, история — в git |

## Лицензия

MIT
