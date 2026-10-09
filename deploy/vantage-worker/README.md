# A second place that checks the website

The status page's own monitor sits on one connection. This Worker loads
`https://yourbot.gg/readiness` once a minute from Cloudflare's network and
sends what it saw to the status service. With it reporting, the page counts
the website as down only when most places cannot reach it, and one bad route
(the monitor's own included) stops showing up as an outage.

It costs nothing on the free plan: one run a minute is 1,440 a day against a
limit of 100,000.

## Deploy

On the status server, once (skip if `INGEST_VANTAGE_SECRET` is already set):

```bash
echo "INGEST_VANTAGE_SECRET=$(openssl rand -hex 32)" | sudo tee -a /etc/status/.env >/dev/null
sudo systemctl restart status-compose
sudo grep '^INGEST_VANTAGE_SECRET=' /etc/status/.env      # copy the value for the next step
```

From this folder, on a machine with Node:

```bash
npx wrangler login                               # opens Cloudflare in the browser
npx wrangler secret put INGEST_VANTAGE_SECRET    # paste the value from above
npx wrangler deploy
```

## Check that it works

Within two minutes:

```bash
curl -s https://status.yourbot.work/api | jq .meta.places
```

lists `cloudflare` next to `home`, and the status page says the website is
checked from 2 places. `npx wrangler tail` shows one line per run:

```json
{"vantage":"cloudflare","saw":"operational","http":200,"ms":183,"reported":200}
```

`"reported":401` means the two secrets differ. `"reported":404` means the
status service has no `INGEST_VANTAGE_SECRET` (or one under 32 characters).

## More places

Each place needs its own name. To add one, deploy the same code again under
another name with its own `VANTAGE_NAME` and `VANTAGE_LABEL`, for example
from a second Cloudflare account or with the checker rewritten for another
provider. The status service accepts up to 8 and only needs the report
described in the main README ("Reports sent to this service").

## Remove it

`npx wrangler delete`. The page stops naming the place ten minutes after its
last report and goes back to its single-monitor rules by itself.
