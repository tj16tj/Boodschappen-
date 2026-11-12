# Boodschappen

Boodschappenlijst die AH en Jumbo vergelijkt, inclusief bonus, en je lijst sorteert op looproute.

- `index.html` — de app (alles in één bestand; je lijst wordt bewaard op je telefoon)
- `scripts/update_data.py` — haalt prijzen (Checkjebon) en bonus (AH/Jumbo app-API) op
- `.github/workflows/update.yml` — draait dat elke ochtend en publiceert de app op GitHub Pages

## Instellen (eenmalig)

1. Zet deze bestanden in een **openbare** GitHub-repo, bijvoorbeeld `Boodschappen-`.
   (Een openbare repo is nodig voor gratis GitHub Pages. Er staat niets persoonlijks in: je lijst blijft op je telefoon.)
2. Ga in de repo naar **Settings → Pages** en kies bij *Source*: **GitHub Actions**.
3. Ga naar **Actions → Prijzen en bonus verversen → Run workflow**.
4. Na een paar minuten staat de app op https://tj16tj.github.io/Boodschappen-/.
   Open hem op je telefoon en kies *Zet op beginscherm*.

## Als de bonus niet meer werkt

De AH- en Jumbo-API's zijn onofficieel en veranderen soms. Onderaan de app staat hoeveel
aanbiedingen er gevonden zijn en of er fouten waren. In `data/status.json` en `data/debug/`
staan de details; daarmee is het script meestal snel aan te passen. Zolang de nieuwe bonus
niet lukt, houdt het script de vorige aan tot de einddatum.

## Lokaal proberen

```
python scripts/update_data.py      # ververst data/
python -m http.server              # open http://localhost:8000
```
