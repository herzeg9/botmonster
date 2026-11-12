# botmonster

Monitor de preço do energético Monster. Cada execução percorre as lojas de
`SITES_CONFIG` (`config_sites.py`), grava `historico_precos.json` e publica
`ofertas.json` (schema v1) para o app Android. O alerta no Telegram sai em HTML
quando o preço por lata muda e fica até R$ 8,50.

O histórico do feed é uma lista plana
`{loja, sabor, nome, pack_qtd, preco_lata, visto_em, id}`.

| Loja | Estratégia |
| --- | --- |
| `pao_de_acucar`, `tauste`, `sams_club` | HTML (Playwright) |
| `amazon` | Playwright, pack de 6, preço por lata |
| `shopee` | API de afiliados; sem secret, a loja é pulada |
| `mercado_livre` | API de catálogo (`tipo: api`) |

## Mercado Livre

A busca pública (`/sites/MLB/search`) continua 403 mesmo com token, e o HTML de
busca cai na verificação anti-bot. Esta loja **não raspa HTML**. Usa só a API
de catálogo, com um token novo a cada execução:

1. `POST https://api.mercadolibre.com/oauth/token` (`grant_type=client_credentials`).
   O `access_token` vale cerca de 21600 s e não é reaproveitado na rodada seguinte.
2. `GET /products/search?site_id=MLB&q=monster%20energy%20473ml&status=active`.
3. `GET /products/{id}/items` para preço, frete e, se vier, `permalink`.
4. `GET /products/{id}` quando a busca não traz `pictures` ou atributos.

Entra no `ofertas.json` no mesmo formato das outras lojas (`loja` =
`mercado_livre`):

- só Monster 473 ml (`domain_id` `MLB-ENERGY_DRINKS` quando o campo vem, marca e
  nome). Outro volume (250 ml, 355 ml, kit misto) fica de fora;
- pack lido do título do item ou do produto (`pack`, `kit`, `cx`, `c/6`,
  `com 6`, `6 unidades`, `12x`, `6x473ml`). Sem indício, a quantidade é 1;
- `preco_lata = (price + frete) / quantidade`. `free_shipping: true` usa frete
  0. Se `shipping.cost` não vier, o frete fica `null` (desconhecido) e a conta
  usa só o `price`;
- uma oferta por produto de catálogo: a de menor preço por lata;
- no máximo 20 ofertas publicadas por execução. A busca lê uma página de até
  50; produto sem anúncio não ocupa vaga, e o nome ganha o prefixo ``Monster``
  quando a marca só veio no atributo;
- link `https://www.mercadolivre.com.br/p/{catalog_product_id}`, ou o
  `permalink` do item quando a API manda;
- imagem, se o produto trouxer `pictures`.

Sem `ML_CLIENT_ID` e `ML_CLIENT_SECRET` o monitor registra um aviso e segue
para as outras lojas, no mesmo stand-by da Shopee. Timeout de 20 s por
request e um retry leve em 429, 5xx ou queda de rede. 401/403 e 429 esgotado
pulam a loja inteira, então uma rodada incompleta não apaga o feed anterior.

### Cadastrar as secrets

No [painel de desenvolvedores do Mercado Livre](https://developers.mercadolivre.com.br),
crie uma aplicação e copie o Client ID e o Client Secret (App ID e Secret Key).
O grant é `client_credentials`: não precisa de redirect de usuário.

**Na sua máquina:** copie `.env.example` para `.env` e preencha
`ML_CLIENT_ID` e `ML_CLIENT_SECRET`. O `.env` não entra no git. O `main.py`
carrega esse arquivo sem sobrescrever variável que já estiver no ambiente.

**No GitHub Actions:** Settings → Secrets and variables → Actions →
New repository secret. Crie as duas:

| Name | Value |
| --- | --- |
| `ML_CLIENT_ID` | Client ID da aplicação |
| `ML_CLIENT_SECRET` | Client Secret da aplicação |

O workflow `.github/workflows/price-monitor.yml` já entrega as duas ao job:

```yaml
ML_CLIENT_ID: ${{ secrets.ML_CLIENT_ID }}
ML_CLIENT_SECRET: ${{ secrets.ML_CLIENT_SECRET }}
```

As secrets do Telegram (`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`) e da Shopee
(`SHOPEE_AFFILIATE_APP_ID`, `SHOPEE_AFFILIATE_SECRET`) ficam no mesmo lugar.

## Testes

Sem rede:

```bash
python -m unittest discover -s tests
```
