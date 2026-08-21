#!/usr/bin/env python3
"""ui.py — every byte of Swedish copy, as stdlib string templates.

Rules baked into this file rather than left to taste:
  * One <h1> per page, and it states an OUTCOME ("Hittade 11 av 26 låtar"), never a
    label ("Resultat").
  * No percentages anywhere. "11 av 26" is a thing he can picture; "42 %" is not.
  * The primary action sits in a sticky footer so it is reachable without scrolling
    past a long track list on a phone.
  * Numbers get a Swedish thousands separator (45 651) with a non-breaking space.
  * Vocabulary he never has to decode: no "misslyckades", "matchning", "API",
    "token", "index" or "Subsonic". The word "fel" appears in exactly one place —
    the wrong-credentials message, where it is the plainest word available.
  * No inline <style> or <script>, so the CSP needs no 'unsafe-inline'.
"""
import html

NBSP = " "


def n(value):
    """45651 -> '45 651' with a non-breaking space, as Swedish is written."""
    return f"{int(value):,}".replace(",", NBSP)


def esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def page(title, body, *, footer="", refresh=None, back=None):
    meta_refresh = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    back_link = (f'<a class="back" href="{esc(back)}">Tillbaka</a>' if back else "")
    return f"""<!doctype html>
<html lang="sv">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
{meta_refresh}
<title>{esc(title)} · Spellistor</title>
<link rel="stylesheet" href="/web/style.css">
</head>
<body>
<main>
{back_link}
{body}
</main>
{f'<div class="bar">{footer}</div>' if footer else ''}
<script src="/web/app.js" defer></script>
</body>
</html>"""


# ---------- 1. login ----------

def login(csrf="", error=None, locked_seconds=None):
    if locked_seconds:
        minutes = max(1, round(locked_seconds / 60))
        note = (f'<p class="warn">För många försök. Vänta {minutes}'
                f'{NBSP}minut{"er" if minutes > 1 else ""} och prova igen.</p>')
    elif error:
        note = f'<p class="warn">{esc(error)}</p>'
    else:
        note = ""
    return page("Logga in", f"""
<h1>Lägg in en spellista i musiksamlingen</h1>
<p class="lead">Logga in med samma namn och lösenord som du använder i Symfonium.</p>
{note}
<form method="post" action="/logga-in" autocomplete="on">
  <input type="hidden" name="csrf" value="{esc(csrf)}">
  <label class="field">
    <span>Användarnamn</span>
    <input name="anvandarnamn" type="text" autocomplete="username"
           autocapitalize="none" autocorrect="off" spellcheck="false" required autofocus>
  </label>
  <label class="field">
    <span>Lösenord</span>
    <input name="losenord" type="password" id="pw" autocomplete="current-password" required>
  </label>
  <label class="check">
    <input type="checkbox" id="showpw"> Visa lösenordet
  </label>
  <button class="primary" type="submit">Logga in</button>
</form>
""")


# ---------- 2. choose a file ----------

def choose(session, csrf, suggestion=""):
    return page("Välj spellista", f"""
<h1>Hej {esc(session.username)}, välj din spellista</h1>
<p class="lead">Välj filen med din spellista här.</p>
<p class="hint">Från Musik på datorn: exportera och välj <strong>Vanlig text</strong>
— då följer alla låtar med. Från Spotify: en <strong>.csv</strong> från
exportify.net fungerar också.</p>

<form method="post" action="/ladda-upp" enctype="multipart/form-data" id="upform">
  <input type="hidden" name="csrf" value="{esc(csrf)}">
  <label class="filebtn">
    <input type="file" name="fil" id="fil">
    <span id="filelabel">Välj fil från telefonen eller datorn</span>
  </label>
  <p class="chosen" id="chosen" hidden></p>

  <label class="field">
    <span>Namn på spellistan</span>
    <input name="namn" type="text" id="namn" value="{esc(suggestion)}"
           maxlength="100" placeholder="Fylls i från filnamnet">
  </label>

  <details class="alt">
    <summary>Eller klistra in låtarna som text</summary>
    <p class="hint">En låt per rad, som <em>Nationalteatern - Livet är en fest</em>.</p>
    <textarea name="text" rows="6" placeholder="Artist - Låt"></textarea>
  </details>

  <button class="primary" type="submit" id="upbtn">Fortsätt</button>
</form>

<form method="post" action="/logga-ut" class="quiet">
  <input type="hidden" name="csrf" value="{esc(csrf)}">
  <button type="submit" class="link">Logga ut</button>
</form>
""")


# ---------- 3. working ----------

def working(job, read_so_far=0):
    if job.step == "laser":
        detail = (f"Läst {n(read_so_far)} låtar …" if read_so_far
                  else "Läser musiksamlingen …")
    else:
        detail = "Letar efter dina låtar i samlingen …"
    return page("Ett ögonblick", f"""
<h1>Ett ögonblick</h1>
<p class="lead">{esc(detail)}</p>
<p class="spinner" aria-hidden="true"></p>
<p class="hint">Sidan uppdaterar sig själv. Du behöver inte göra något.</p>
""", refresh=2)


# ---------- 4. preview ----------

def _pair_row(source, found):
    src_artist = ", ".join(source.get("artists") or []) or "?"
    src = f"{src_artist} – {source.get('name', '')}"
    lib = f"{found.get('artist', '?')} – {found.get('title', '?')}"
    second = ""
    if lib.casefold() != src.casefold():
        album = f" ({found['album']})" if found.get("album") else ""
        second = f'<span class="as">I samlingen: {esc(lib)}{esc(album)}</span>'
    return f"<li>{esc(src)}{second}</li>"


def _miss_row(source):
    artist = ", ".join(source.get("artists") or []) or "?"
    return f"<li>{esc(artist)} – {esc(source.get('name', ''))}</li>"


def preview(job, csrf, filename=""):
    found_n, total = len(job.pairs), job.source_total
    missing_n = len(job.misses)

    if found_n == 0:
        body = f"""
<h1>Ingen av låtarna finns i musiksamlingen</h1>
<p class="lead">Ingenting har ändrats.</p>
<p>Det brukar betyda att låtarna inte är inlagda i samlingen ännu.</p>
<details open><summary>Låtarna du skickade ({n(total)})</summary>
<ul class="tracks">{''.join(_miss_row(m) for m in job.misses)}</ul></details>
"""
        return page("Inga låtar hittades", body, footer=(
            '<a class="primary" href="/ny">Välj en annan fil</a>'), back="/ny")

    reason = ""
    if missing_n == 1:
        reason = ('<p class="lead">En låt finns inte i musiksamlingen, så den kan '
                  "inte läggas till. Det går bra att skapa spellistan med de "
                  f"{n(found_n)} som finns.</p>")
    elif missing_n:
        reason = (f'<p class="lead">{n(missing_n)} låtar finns inte i '
                  "musiksamlingen, så de kan inte läggas till. Det går bra att "
                  f"skapa spellistan med de {n(found_n)} som finns.</p>")
    else:
        reason = '<p class="lead">Alla låtar finns i musiksamlingen.</p>'

    hint = ""
    if filename.lower().endswith((".m3u", ".m3u8")) and found_n * 2 < total:
        hint = ('<p class="warn">Den här filtypen tar bara med låtar som finns '
                "som filer på din dator. Exportera som <strong>Vanlig text</strong> "
                "i stället, då följer alla låtar med.</p>")

    if job.existing:
        consequence = (f'<p class="warn">Du har redan en spellista som heter '
                       f'”{esc(job.target_name)}”. Låtarna läggs till i den. '
                       "Inget tas bort.</p>")
        button = "Lägg till i spellistan"
    else:
        consequence = ""
        button = "Skapa spellistan"

    misses_block = ""
    if missing_n:
        heading = ("Visa låten som saknas" if missing_n == 1
                   else f"Visa de {n(missing_n)} låtar som saknas")
        misses_block = f"""
<details><summary>{heading}</summary>
<ul class="tracks">{''.join(_miss_row(m) for m in job.misses)}</ul></details>"""

    body = f"""
<h1>Hittade {n(found_n)} av {n(total)} låtar</h1>
{reason}
{hint}
{consequence}
<details open><summary>{"Låten som läggs in" if found_n == 1 else f"Låtar som läggs in ({n(found_n)})"}</summary>
<ul class="tracks">{''.join(_pair_row(s, f) for s, f, _ in job.pairs)}</ul></details>
{misses_block}
"""
    footer = f"""
<form method="post" action="/skapa">
  <input type="hidden" name="csrf" value="{esc(csrf)}">
  <input type="hidden" name="jobb" value="{esc(job.job_id)}">
  <input type="hidden" name="bekrafta" value="{esc(job.commit_token)}">
  <button class="primary" type="submit">{esc(button)}</button>
</form>"""
    return page("Förhandsvisning", body, footer=footer, back="/ny")


# ---------- 5. done ----------

def done(job):
    res = job.result or {}
    added = res.get("added", 0)
    name = job.target_name
    if res.get("nothing_new"):
        body = f"""
<h1>”{esc(name)}” var redan uppdaterad</h1>
<p class="lead">Alla låtar fanns redan i spellistan. Ingenting behövde läggas till.</p>
<p>Du hittar den i Symfonium under dina spellistor.</p>
"""
    else:
        verb = "lades till i" if job.existing else "finns nu i"
        body = f"""
<h1>Klart — ”{esc(name)}” är sparad</h1>
<p class="lead">{n(added)} {"låt" if added == 1 else "låtar"} {verb} spellistan.</p>
<p>Den är <strong>bara din</strong>. Ingen annan som använder musikservern ser den.
Öppna Symfonium och dra nedåt för att uppdatera, så dyker den upp.</p>
"""
    return page("Klart", body, footer='<a class="primary" href="/ny">Lägg in en till</a>')


# ---------- 6. problems ----------

def session_gone(csrf=""):
    """Shown when a POST arrives with a dead session — most often a second tab
    that was left open, or an hour of thinking time. It carries the login form so
    he is one step from continuing rather than hunting for the way back."""
    return page("Logga in igen", f"""
<h1>Du behöver logga in igen</h1>
<p class="lead">Inloggningen hade gått ut, så filen kom inte fram. Ingenting har
ändrats i musiksamlingen.</p>
<p>Logga in och välj filen en gång till.</p>
<form method="post" action="/logga-in" autocomplete="on">
  <input type="hidden" name="csrf" value="{esc(csrf)}">
  <label class="field">
    <span>Användarnamn</span>
    <input name="anvandarnamn" type="text" autocomplete="username"
           autocapitalize="none" autocorrect="off" spellcheck="false" required autofocus>
  </label>
  <label class="field">
    <span>Lösenord</span>
    <input name="losenord" type="password" id="pw" autocomplete="current-password" required>
  </label>
  <label class="check">
    <input type="checkbox" id="showpw"> Visa lösenordet
  </label>
  <button class="primary" type="submit">Logga in</button>
</form>
""")


def problem(message, *, back="/ny", title="Det gick inte"):
    return page(title, f"""
<h1>{esc(title)}</h1>
<p class="lead">{esc(message)}</p>
""", footer=f'<a class="primary" href="{esc(back)}">Försök igen</a>')


def too_big(limit_mb):
    return problem(
        f"Filen är större än {limit_mb}{NBSP}MB. Exportera en spellista i taget, "
        "inte hela musikbiblioteket.",
        title="Filen är för stor")


def expired():
    return problem(
        "Det har gått en stund, så jag har glömt filen. Välj den igen.",
        back="/", title="Börja om")
