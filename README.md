# Bible archive

A local, searchable copy of the highlights and notes on your YouVersion account.

The archive stays in `data/` on this computer. You can search it by words, by a quoted phrase, and by the date each item was created.

## What this reads

YouVersion's public Platform API (`api.youversion.com`) can list highlights after an app registration, but the published highlight record is only a translation, a passage, and a color. It has no created date, and it does not include notes.

The Bible app itself loads your saved items from `moments.youversionapi.com` (`/3.1/items.json`, with your user id, a page number, and a kind such as `highlight` or `note`). Dates, note text, labels, colors, and passage references are on those records. An unofficial Python client, [youversion-bible-client](https://github.com/tushortz/youversion-bible-client), already documents that API, including a small note-to-markdown exporter. This project uses the same read endpoints and none of the write, friend, or messaging ones.

Saved Events are included when that service responds. Notes you typed onto an event are kept. The church's outline and other people's comments are not indexed.

Highlights are stored per translation, so the same verse can appear once for each translation you marked.

Verse text from copyrighted translations is not downloaded. Search covers your note text, titles, labels, and references such as `John 15:1` or `JHN.15.1`.

YouVersion's [privacy policy](https://www.bible.com/privacy) also lets you ask them for a copy of your data: email `help@youversion.com`. That file is a fallback if sign-in here does not work. It is not a search tool.

## Setup

```bash
cd /path/to/bible-app-archiver
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

## Sign in

The first sign-in is in the browser at [https://www.bible.com](https://www.bible.com). Email, Google, and Apple all work on that page. This program never sends your password. `YOUVERSION_PASSWORD` in `.env` is ignored, and you can delete it. YouVersion rejects a password sent by this app with "The client is not authorized to request a token using this method."

After you are signed in on the site, copy one of these into the local session file `data/session.json`. That file is readable only by your user.

### Cookie

Use the `yva_session` cookie if you want later syncs to renew themselves.

1. Open Developer Tools. In Chrome choose Application, then Cookies, then `https://www.bible.com`. In Firefox choose Storage, then Cookies.
2. Copy the value of `yva_session`.
3. Run login and paste `yva_session=` followed by the value. The paste is hidden.

```bash
python -m bible_archive login
```

You can instead set `YOUVERSION_SESSION_COOKIE` in `.env` to the value alone. Sync calls `https://www.bible.com/api/authenticate` with the cookie when the access token is inside 10 minutes of expiry. The token bible.com returns lasts 96 hours. The cookie keeps working until bible.com rejects it. Then sign in on the site again and copy `yva_session` once more.

Renew the token without downloading highlights:

```bash
python -m bible_archive auth-refresh
```

`auth-refresh` only replaces the access token.

### Access token

A pasted access token works for one stretch of 96 hours, measured from when bible.com issued it. It has no refresh token. When it expires, paste a cookie or a new token.

In the Console on bible.com, while you are signed in, paste:

```javascript
copy(JSON.stringify(await (await fetch("/api/authenticate", {cache:"no-store"})).json()))
```

Put that JSON in `YOUVERSION_ACCESS_TOKEN`, or paste it at the `login` prompt. `auth-refresh` cannot renew this by itself.

## Download

Start with one page so you can confirm the shape before a long run:

```bash
python -m bible_archive sync --max-pages 1
python -m bible_archive peek
python -m bible_archive sync
```

Requests are one at a time, with 1.5 seconds between them. On HTTP 429 the tool waits for `Retry-After` and tries again. A page that comes back `not_found`, or that says there is no next page, ends that list. Saved pages are reused only after a sync has downloaded that whole list. The next sync compares the first and last saved page. When both still match, the pages in between are kept. When either one differs, that list is downloaded again, and highlights or notes you removed are deleted from `data/raw/` and the catalog. `--redownload` downloads every page again, which also picks up an edit that did not add or remove an item. `--delay` can be raised. It will not go below 0.5 seconds.

A library built over many years can take a while. That is intentional.

`peek` prints how many items were on each page and which fields exist. It does not print note text.

## Search

Open the browser page:

```bash
python -m bible_archive serve
```

Then go to [http://127.0.0.1:8765](http://127.0.0.1:8765). The page is served only on this computer. It has a search box, type checkboxes, a translation filter, from/to dates, and a month list.

From the terminal:

```bash
python -m bible_archive search grace
python -m bible_archive search '"the vine"'
python -m bible_archive search --month 2016-05 --kind note
python -m bible_archive search --from 2014-01-01 --to 2014-12-31 --oldest
```

Every word has to appear. A quoted phrase has to appear as written. Matching is not case-sensitive. `%` and `_` are ordinary characters, not wildcards.

Export a JSON file and a CSV file:

```bash
python -m bible_archive export
```

The files go to `data/export/`. The raw API responses remain in `data/raw/` so the catalog can be rebuilt without downloading again:

```bash
python -m bible_archive rebuild
```

## Tests

```bash
python -m unittest discover -s tests
```

The tests use fake API responses. They do not contact YouVersion.

## Sources

The moments, labels, events, and Bible-version reads, and the nested note shape, follow [youversion-bible-client](https://github.com/tushortz/youversion-bible-client) by Taiwo Kareem:

- [API docs](https://github.com/tushortz/youversion-bible-client/blob/main/DOCS.md)
- [`note_to_markdown.py`](https://github.com/tushortz/youversion-bible-client/blob/main/note_to_markdown.py), for `object.content`, `created_dt`, and `references`
- [Read the Docs](https://youversion-bible-client.readthedocs.io/en/latest/)
- [Changelog](https://github.com/tushortz/youversion-bible-client/blob/main/CHANGELOG.md), which notes that YouVersion has made parts of this API private

That project also signs in with a password grant and can write moments, manage friends, and register a device. This archive uses its read endpoints for moments, labels, saved events, and version names. Password sign-in to `https://auth.youversionapi.com/token` now returns "The client is not authorized to request a token using this method."

The public Platform docs describe a different API, at `https://api.youversion.com`:

- [Authentication](https://developers.youversion.com/authentication) and the [quick reference](https://developers.youversion.com/quick-reference) use an app key
- [Sign-in APIs](https://developers.youversion.com/sign-in-apis) use OAuth with PKCE at `POST /auth/token`
- [Data exchange](https://developers.youversion.com/api/data-exchange) can share highlights from a signed-in user
- [Schemas](https://developers.youversion.com/api/~schemas) show a highlight as a Bible id, a passage, and a color

A Platform highlight is a Bible id, a passage, and a color. This archive reads `moments.youversionapi.com` because that feed includes created dates and the notes you typed. The same OAuth change is in [youversion/platform-sdk-swift](https://github.com/youversion/platform-sdk-swift/commit/c6216bdc5ed42f5518d60ab357f592439b0c097e).

A 2023 [gist by dtxe](https://gist.github.com/dtxe/9b8e1426bf71edb1248a2f185453c2cb) shows a signed-in bible.com token sent as `Authorization: Bearer` to `*.youversionapi.com`, stored at that time in a cookie named `yva`. In October 2026, [www.bible.com](https://www.bible.com/) keeps an httpOnly cookie named `yva_session` and returns the bearer token from [`/api/authenticate`](https://www.bible.com/api/authenticate). The web reader sends that token to `https://moments.youversionapi.com/3.1/` with `X-YouVersion-App-Platform: web`, `X-YouVersion-App-Version: 3`, and `Referer: https://web.youversionapi.com`. Account sign-in is [login.youversion.com](https://login.youversion.com/).

These other pages cover Bible text or an older developer-token API:

- [youversion-public-api-docs](https://github.com/ob1Shaun/youversion-public-api-docs)
- [YouVersion-API](https://github.com/Glowstudent777/YouVersion-API)
- [youversion](https://github.com/PandaWhoCodes/youversion), a client for the Platform Bible API

A personal-data copy can be requested under the [privacy policy](https://www.bible.com/privacy) ("Right to Access and/or Take Your Data") by emailing `help@youversion.com`.

## License

This project is licensed under the [MIT License](LICENSE).
