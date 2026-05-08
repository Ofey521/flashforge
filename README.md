# anki-creator

Headless mikroserwis dodający notatki do kolekcji Anki przez HTTP API. Używa pakietu Pythona `anki` (silnik bez GUI), synchronizuje się z AnkiWeb. Przeznaczony do współpracy z n8n (lub dowolnym workflow runnerem).

## Architektura

```
Telegram ──▶ n8n ──HTTP──▶ anki-creator ──sync──▶ AnkiWeb
                               │                     ▲
                               ▼                     │
                         collection.anki2       Anki desktop / mobile
                         (volume)
```

- Brak Qt, Xvfb, AnkiConnect, noVNC. Tylko Python + FastAPI + `anki`.
- Kolekcja trzymana lokalnie w wolumenie, sync z AnkiWeb na żądanie (`POST /sync`).
- Kontener działa jako non-root user (uid 1000).

## Workflow Telegram → Anki

W katalogu `n8n/` znajdują się gotowe workflow n8n:

| plik | opis |
| --- | --- |
| `workflow1-word-generator.json` | Codzienny generator "słowa dnia" (schedule → AI → Postgres → email) |
| `workflow2-notification.json` | Notyfikacja email ze słówkiem dnia + podsumowanie tygodnia |
| `workflow3-telegram-anki.json` | **Główny workflow**: Telegram → AI → Anki |

### workflow3 — jak działa

```
Telegram → GPT-4o-mini (tłumaczenie + walidacja) → Parser → Is valid?
  → TAK → Reply OK (natychmiastowa odpowiedź) → Check duplicate → Is new word?
              → TAK → GPT-5-mini (IPA + examples) → Build card → add_notes → sync
              → NIE → Reply "karta już istnieje"
  → NIE → Reply "nie rozpoznaję słowa"
```

- **GPT-4o-mini** — szybkie tłumaczenie EN↔PL, walidacja słowa, autokorekta literówek
- **GPT-5-mini** — generowanie pełnej karty: transkrypcja IPA, 3 przykłady + tłumaczenia
- Odpowiedź na Telegramie leci natychmiast po tłumaczeniu, karta Anki tworzy się w tle

## Wymagania

- Docker + Docker Compose
- Konto AnkiWeb
- n8n (osobny kontener) z credentials: Telegram API, OpenAI Bearer, Anki API Key

## Szybki start

```bash
# 1. Konfiguracja
cp .env.example .env
echo "API_KEY=$(openssl rand -hex 32)" >> .env
nano .env   # uzupełnij ANKIWEB_USERNAME, ANKIWEB_PASSWORD

# 2. Uprawnienia dla danych (uid 1000 = user anki w kontenerze)
mkdir -p data && chown 1000:1000 data/

# 3. Build i start
docker compose build
docker compose up -d

# 4. Podłącz do sieci n8n (żeby n8n widział anki-creator)
docker network connect n8n-compose_default anki-creator

# 5. Pierwszy sync (ściągnie kolekcję z AnkiWeb)
curl -X POST http://localhost:8000/sync \
  -H "X-API-Key: $(grep ^API_KEY .env | cut -d= -f2)"
# => {"status":"full_download"}
```

### Zmienne środowiskowe

| zmienna | opis |
| --- | --- |
| `ANKIWEB_USERNAME` | email konta AnkiWeb |
| `ANKIWEB_PASSWORD` | hasło konta AnkiWeb |
| `API_KEY` | sekret do nagłówka `X-API-Key` (wygeneruj: `openssl rand -hex 32`) |
| `ANKIWEB_ENDPOINT` | puste = oficjalny AnkiWeb |

## Podpięcie n8n

n8n musi widzieć kontener `anki-creator` po sieci Docker:

```bash
docker network connect n8n-compose_default anki-creator
```

**Uwaga:** po każdym `docker compose up -d` (recreate kontenera) trzeba ponownie podłączyć sieć.

W n8n utwórz credential "Header Auth" z `X-API-Key` = wartość `API_KEY` z `.env`. W workflow HTTP Request nodes kieruj na `http://anki-creator:8000`.

## API

Wszystkie endpointy poza `/healthz` wymagają nagłówka `X-API-Key`.

### `GET /healthz`
Liveness probe — sprawdza dostępność kolekcji Anki. Bez auth.

### `GET /decks`
```json
{"decks": ["Default", "zbocznica::n8n"]}
```

### `GET /models`
```json
{"models": ["Podstawowy", "Podstawowy (z odwrotną kartą)", "Luka"]}
```

### `GET /models/{name}/fields`
```json
{"fields": ["Przód", "Tył"]}
```

### `POST /can_add`
Pre-check: które notatki da się dodać.
```json
{
  "notes": [
    {"deck": "zbocznica::n8n", "model": "Podstawowy (z odwrotną kartą)",
     "fields": {"Przód": "ephemeral", "Tył": "ulotny"}, "tags": ["telegram"]}
  ]
}
```
```json
{"results": [{"can_add": true, "reason": null}]}
```

### `POST /add_notes`
```json
{
  "notes": [
    {"deck": "zbocznica::n8n", "model": "Podstawowy (z odwrotną kartą)",
     "fields": {"Przód": "<b>ephemeral</b> <i>/ɪˈfem.ər.əl/</i>", "Tył": "<b>ulotny</b>"},
     "tags": ["telegram"]}
  ],
  "allow_duplicate": false
}
```
```json
{"added": [1730000000001], "skipped": []}
```

### `POST /find_notes`
Wyszukiwanie notatek (składnia Anki search).
```json
{"query": "deck:zbocznica::n8n Przód:*ephemeral*"}
```
```json
{"count": 1, "notes": [{"id": 123, "fields": {"Przód": "...", "Tył": "..."}, "tags": ["telegram"]}]}
```

### `POST /sync`
Synchronizacja z AnkiWeb. Opcjonalny `force_direction`: `"upload"` | `"download"`.
```json
{"status": "synced"}
```

## Operacje

### Rebuild po zmianach

```bash
docker compose build && docker compose up -d
docker network connect n8n-compose_default anki-creator
```

### Logi

```bash
docker compose logs -f anki-creator
```

Rotacja logów skonfigurowana: max 3 pliki po 10 MB.

### Backup

Kolekcja: `./data/collection.anki2`. AnkiWeb to nie backup — kasacja na jednym kliencie propaguje się synciem.

```bash
0 3 * * * tar czf /backups/anki-$(date +\%F).tar.gz -C /home/USER/anki-creator data/
```

### Update wersji `anki`

Wersja w `requirements.txt` musi być **<= wersji Anki desktop**. Nowsza biblioteka zapisze schemat, którego stary desktop nie odczyta.

```bash
docker compose build --no-cache && docker compose up -d
```

## Troubleshooting

| problem | rozwiązanie |
| --- | --- |
| `401 invalid api key` | Sprawdź header `X-API-Key`, porównaj z `.env` |
| `collection is locked` | Restart kontenera: `docker compose restart` |
| Sync zwraca `full_upload` | Lokalna kolekcja odeszła za daleko od serwera — zrób backup `data/`, zdecyduj kierunek |
| `unknown model: X` | Sprawdź `GET /models`, nazwy są case-sensitive |
| Karta nie pojawia się na telefonie | Czy wywołałeś `/sync` po `add_notes`? Czy telefon się zsynchronizował? |
| n8n nie widzi anki-creator | `docker network connect n8n-compose_default anki-creator` |

## Bezpieczeństwo

- Port 8000 bindowany na `127.0.0.1` — niedostępny z zewnątrz.
- `API_KEY` to jedyna autoryzacja. `.env` jest w `.gitignore`.
- `.env` zawiera hasło w plaintext — chroń uprawnienia: `chmod 600 .env`.
- Kontener działa jako non-root user (uid 1000).

## Licencja

Pakiet `anki` jest na licencji AGPL-3.0 — co wpływa na warunki dystrybucji tego serwisu jeśli byłby upubliczniany.
