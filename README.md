# Noah Classico — mini-webshop met bieden

Een simpele Flask-app om meubels te plaatsen, biedingen te ontvangen en (optioneel) e-mailnotificaties te versturen.

## Snel starten (lokaal)

1. Zorg dat Python 3.10+ is geïnstalleerd.
2. Open een terminal in deze map en voer uit:

```bash
python -m venv .venv
source .venv/bin/activate  # op Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env  # vul de waarden in .env aan
python app.py
```

3. Ga naar http://localhost:5000 in je browser.
4. Upload items via http://localhost:5000/admin (wachtwoord in `.env`: ADMIN_PASSWORD).

## E-mailnotificaties

Vul in `.env` de SMTP-gegevens in. Bij elk nieuw bod wordt een mail gestuurd naar `ADMIN_EMAIL`. Zonder SMTP-configuratie draait de app gewoon door, maar slaat e-mail over.

## Deploy opties

- **Render/Railway/Fly.io**: push deze map naar een Git-repo en maak een nieuwe web service. Zorg dat `PORT` en `.env` zijn ingesteld.
- **Docker** (optioneel): maak een eenvoudige Dockerfile aan en run op een VPS.

## Veiligheid

Deze demo heeft minimale beveiliging (één admin-wachtwoord en geen CSRF). Voor productie: voeg echte auth toe, beperk admin-routes, gebruik HTTPS en een sterk `SECRET_KEY`.

Veel succes met Noah Classico! ✨
