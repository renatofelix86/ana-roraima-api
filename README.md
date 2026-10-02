# API Hidrologia Roraima

Coleta os dados de nível de água das estações da ANA (HidroWebService),
armazena em PostgreSQL, mantém tudo atualizado sozinho e entrega uma camada
pronta para dashboards do Power BI.

Para colocar em produção e atualizar depois, veja **[DEPLOY.md](DEPLOY.md)**.

## Componentes

| Serviço  | Papel |
|----------|-------|
| `db`     | PostgreSQL 16. Guarda a série e publica as views `bi_*`. |
| `worker` | Autentica na ANA e coleta: inventário, telemetria e série histórica. |
| `api`    | FastAPI somente-leitura, para o sistema da corporação. |

O Power BI **não** passa pela API: ele lê o PostgreSQL direto, pelas views
`bi_*`. Ver [Power BI](#power-bi).

## Modelo de dados

Tudo cai em `measurements`, distinguido por três campos:

| `source` | `reading_kind` | `consistency_level` | origem |
|---|---|---|---|
| `ANA_TELEMETRIA` | `TELEMETRIA` | `0` | leitura automática, ~15 em 15 min |
| `ANA_COTAS` | `MEDIA_DIARIA` | `1` / `2` | média diária da série histórica |
| `ANA_COTAS` | `LEITURA_REGUA` | `1` / `2` | leitura manual de régua (7h / 17h) |

`consistency_level` é o `nivelconsistencia` da ANA: **1 = bruto**,
**2 = consistido** (revisado pela agência). Os dois coexistem para o mesmo dia
— numa estação de teste havia 20.850 médias diárias brutas e 20.665
consistidas — e por isso os cinco campos
(`station_code`, `measured_at`, `source`, `reading_kind`, `consistency_level`)
formam a chave natural. Onde há as duas versões, as views de BI preferem a
consistida.

### Fusos horários

Ponto delicado, porque a ANA devolve datas sem indicação de fuso:

- **Telemetria**: carimbada pelo servidor da ANA em **UTC-3** (Brasília).
  Verificado consultando estações de RR (UTC-4), AC (UTC-5) e SP (UTC-3) no
  mesmo instante: todas devolveram o mesmo horário, provando fuso único de
  servidor, e só UTC-3 é compatível com a hora real.
- **Série histórica**: as leituras de 7h e 17h são horários *locais* de
  observação, então são interpretadas em `LOCAL_TZ`.

No banco tudo é `timestamptz` em UTC. Para os relatórios existe
`measured_date`, o dia-calendário de Roraima, e as views expõem horários já
convertidos. Sem isso, todo dashboard ficaria **1 hora adiantado**.

## 1. Configuração

```bash
cp .env.example .env
```

Preencha as credenciais da ANA e as senhas. **Nunca faça commit do `.env`.**

## 2. Subir

```bash
docker compose up -d --build
docker compose logs -f worker
```

O worker já coleta no primeiro minuto: inventário na hora, telemetria 30s
depois e a carga histórica 2 minutos depois.

## 3. Carga histórica

Roda sozinha (job `backfill_cotas`, a cada `BACKFILL_INTERVAL_HOURS`), mas dá
para forçar:

```bash
# tudo que faltar, retomando de onde parou
docker compose run --rm worker python -m app.backfill

# uma estação só
docker compose run --rm worker python -m app.backfill --station 14710000

# pula as já concluídas
docker compose run --rm worker python -m app.backfill --pending
```

A ANA limita cada consulta a 366 dias, então a carga anda **ano a ano**, e
cada ano é gravado antes do próximo. Interromper com Ctrl+C é seguro: o
progresso por estação fica em `backfill_progress` (view `bi_status_backfill`)
e a próxima execução continua dali.

O ano inicial de cada estação sai do inventário
(`Data_Periodo_Escala_Inicio` e afins), evitando varrer décadas vazias; quando
o inventário não informa, usa-se `BACKFILL_START_YEAR`. Uma estação com série
desde 1967 rende cerca de 74 mil observações.

Toda gravação é `UPSERT` pela chave natural: reprocessar não duplica.

### Volume de requisições à ANA

Medido durante a carga completa de RR (69 estações, série desde 1960):

| | |
|---|---|
| Carga histórica completa | **3.234 requisições**, uma única vez |
| Pico observado | **~111 req/min** (~1,9/s), carga + telemetria juntas |
| Regime permanente | ~2.200 req/dia (telemetria) + ~69/dia (revisão do histórico) |

Em 3.243 requisições **não houve nenhum HTTP 429**, e a ANA **não devolve
cabeçalhos de limite** (`X-RateLimit-*`, `Retry-After`) — não há limite
declarado por máquina a respeitar. Confira os termos de uso da ANA antes de
subir muito o ritmo.

Ocorreram 4 respostas **504** (todas em `HidroSerieCotas`, o endpoint pesado),
espalhadas ao longo de 45 min em vez de agrupadas — comportamento de timeout de
consulta do lado da ANA, não de bloqueio por excesso. O retry com backoff
exponencial de `_get` absorveu todas: nenhuma estação ficou com erro.

Para aliviar o ritmo, aumente `BACKFILL_REQUEST_DELAY` (padrão `0.35` s entre
requisições) ou desligue com `BACKFILL_ENABLED=false`.

**Validade do token**: o manual da ANA fala em 60 min, mas na prática é ~45 —
um token de 17:28:55 foi recusado às 18:13:54. O cliente renova aos 40 min por
isso. Se voltar a aparecer `401` periódico no log, reduza mais um pouco em
`app/ana_client.py`.

## 4. Power BI

O Power BI conecta **direto no PostgreSQL**, não na API. Isso libera refresh
incremental e DirectQuery, e evita a dor de cabeça de header de autenticação e
paginação do `Web.Contents`.

### 4.1 Usuário somente-leitura

Defina `BI_READONLY_PASSWORD` no `.env` e rode:

```bash
docker compose run --rm api python -m app.bi_user
```

Cria `powerbi_ro` com `SELECT` **apenas nas views `bi_*`** — sem acesso às
tabelas, então nem a coluna `raw` (JSON bruto da ANA) nem escrita ficam
expostas.

### 4.2 Conectar

No Power BI Desktop: **Obter Dados → Banco de Dados PostgreSQL**

| Campo | Valor |
|---|---|
| Servidor | `localhost` — porta `5433` (ou o host da VPS) |
| Banco de dados | `hidrologia` |
| Usuário | `powerbi_ro` |
| Modo | Importação (recomendado) ou DirectQuery |

### 4.3 As views

| View | Para quê |
|---|---|
| `bi_dim_estacao` | Dimensão de estações, com lat/long para o mapa. |
| `bi_dim_calendario` | Tabela-calendário em português, com período hidrológico. |
| `bi_serie_diaria` | **Tabela-fato principal.** Uma linha por estação/dia. |
| `bi_fato_medicao` | Observação a observação, para detalhe intradiário. |
| `bi_ultima_leitura` | Última leitura de cada estação, com atraso em minutos. |
| `bi_referencia_sazonal` | Mediana, p10 e p90 históricos por dia do ano. |
| `bi_status_coleta` | Saúde da coleta. |
| `bi_status_backfill` | Andamento da carga histórica. |

Relacione `bi_serie_diaria[codigo_estacao]` → `bi_dim_estacao[codigo_estacao]`
e `bi_serie_diaria[data_medicao]` → `bi_dim_calendario[data]`.

`bi_referencia_sazonal` é o que permite a pergunta que importa no dashboard:
*o nível de hoje está acima ou abaixo do normal para esta época do ano?* Ela
traz décadas de referência por dia do calendário — numa estação com série
desde 1967, são 59 anos de base para cada dia.

### 4.4 Sobre os horários nas views

Todas as colunas de data/hora das views vêm **naive em hora de Roraima**. É
proposital: o conector Npgsql entrega `timestamptz` como `DateTimeOffset` e o
Power BI converte para o fuso da máquina que faz o refresh — o relatório
mudaria de horário ao ser publicado no Service. Quem precisar do instante
absoluto tem `medido_em_utc` em `bi_fato_medicao`.

### 4.5 Refresh incremental

Em `bi_serie_diaria`, use `data_medicao` como coluna de data, com
`RangeStart`/`RangeEnd`. Como a série histórica só muda quando a ANA consiste
dados antigos, uma janela de ~90 dias de reprocessamento cobre bem.

## 5. pgAdmin

O banco fica publicado em **`127.0.0.1:5433`**, não na 5432.

O motivo: o instalador do pgAdmin costuma trazer junto um PostgreSQL nativo,
que já ocupa a 5432 do Windows como serviço. Tentar publicar o container
naquela porta falha com *port already allocated*, e — pior — conectar o
pgAdmin na 5432 abre o servidor local vazio, não este projeto. A porta do host
é configurável por `DB_HOST_PORT` no `.env`.

No pgAdmin: clique com o botão direito em **Servers → Register → Server**

**Aba General**

| Campo | Valor |
|---|---|
| Name | `ANA Roraima` (qualquer nome) |

**Aba Connection**

| Campo | Valor |
|---|---|
| Host name/address | `127.0.0.1` |
| Port | `5433` |
| Maintenance database | `hidrologia` |
| Username | `hidrologia` (admin) ou `powerbi_ro` (somente-leitura) |
| Password | a do `.env` (`POSTGRES_PASSWORD` / `BI_READONLY_PASSWORD`) |
| Save password | marque, se quiser |

As tabelas ficam em **hidrologia → Schemas → public → Tables** e as views de
BI em **Views**. Para explorar, o caminho mais rápido é a ferramenta de query:

```sql
-- panorama do que já foi coletado
select source, reading_kind, consistency_level, count(*),
       min(measured_date) as de, max(measured_date) as ate
from measurements group by 1,2,3 order by 1,2,3;

-- situação atual das estações
select * from bi_ultima_leitura order by atraso_minutos;

-- andamento da carga histórica
select * from bi_status_backfill order by atualizado_em desc;
```

Use `hidrologia` para administrar e `powerbi_ro` quando só for consultar — ele
enxerga apenas as views `bi_*` e não escreve nada.

## 6. API interna

```bash
curl http://127.0.0.1:8000/health

curl -H "X-API-Key: SUA_APP_API_KEY" \
  "http://127.0.0.1:8000/api/stations?telemetered=true&operating=true"

curl -H "X-API-Key: SUA_APP_API_KEY" \
  "http://127.0.0.1:8000/api/stations/14500000/latest"

curl -H "X-API-Key: SUA_APP_API_KEY" \
  "http://127.0.0.1:8000/api/stations/14500000/daily?start=2024-01-01"
```

Swagger: <http://127.0.0.1:8000/docs>

`/health` não devolve só `ok`: informa a idade da última medição e o resultado
da última coleta — é o que serve para alarme de coleta parada.

`/daily` lê a mesma view `bi_serie_diaria` do Power BI, então API e dashboard
nunca divergem.

## 7. Deploy na VPS

### 7.1 Convivendo com outras aplicações

O projeto foi preparado para dividir a VPS:

- **Nome de projeto fixo** (`name: ana-roraima-api` no compose): containers,
  rede e volume têm nomes previsíveis, independentes da pasta onde o repo foi
  clonado.
- **Portas configuráveis**: `API_HOST_PORT` (padrão 8000) e `DB_HOST_PORT`
  (padrão 5433) no `.env`. Se algo já ocupa essas portas na VPS, troque —
  nada mais precisa mudar.
- **Rotação de log**: `max-size: 10m`, `max-file: 3` por serviço. Sem isso, o
  log do worker (uma linha por requisição à ANA) cresceria sem limite.
- **Rede própria**: o compose cria a sua própria rede; `api` e `worker`
  chegam ao banco por `db:5432`, sem passar pelo host.

### 7.2 Passo a passo

O passo a passo completo — deploy inicial, atualização do código em produção,
rollback e diagnóstico — está em **[DEPLOY.md](DEPLOY.md)**.

Resumo do deploy inicial:

```bash
cp .env.example .env && nano .env    # credenciais e portas livres na VPS
docker compose up -d db               # SO o banco primeiro
./scripts/restore.sh backups/hidrologia-AAAAMMDD-HHMMSS.dump
docker compose up -d
docker compose run --rm api python -m app.bi_user
```

Para atualizar depois, a receita é `backup -> git pull -> build -> up -d`;
os detalhes e as armadilhas estão no DEPLOY.md.

### 7.3 Backup

```bash
./scripts/backup.sh          # gera backups/hidrologia-<data>.dump
```

O `pg_dump` roda dentro do container (ferramentas do PostgreSQL 16 da própria
imagem). Isso evita a armadilha de gerar o dump com o `pg_dump 17` que vem com
o pgAdmin no Windows: dump de versão maior não restaura em servidor menor.

O dump sai em ~18 MB para 1,17 milhão de medições — os índices (268 MB) não
vão no arquivo, são reconstruídos na restauração.

Para backup automático na VPS, um cron diário:

```cron
0 3 * * * cd /caminho/ana-roraima-api && bash scripts/backup.sh >> /var/log/ana-backup.log 2>&1
find /caminho/ana-roraima-api/backups -name '*.dump' -mtime +14 -delete
```

### 7.4 Exposição e segurança

`docker-compose.yml` publica API e banco **apenas em `127.0.0.1`**. Para o
Power BI alcançar o banco a partir de fora, em ordem de preferência:

1. **Túnel SSH** (mais simples e seguro):
   ```bash
   ssh -L 5433:127.0.0.1:5433 usuario@vps
   ```
   O Power BI então aponta para `localhost:5433`.
2. **Firewall restrito**: libere a porta só para os IPs de origem e ative TLS
   no Postgres (`ssl = on`). Com o Power BI Service, será preciso um
   *on-premises data gateway*.

Nunca troque o bind para `0.0.0.0` sem uma das duas medidas acima. Para a API,
use Nginx/Traefik/Caddy com HTTPS na frente e mantenha o `X-API-Key`.

## 8. Diagnóstico

```bash
# credenciais e inventário
docker compose run --rm api python -m app.check_ana

# uma consulta de telemetria
docker compose run --rm api python -m app.check_telemetry

# saúde da coleta
docker compose exec db psql -U hidrologia -d hidrologia \
  -c "select * from bi_status_coleta order by iniciado_em desc limit 10;"

# andamento da carga histórica
docker compose exec db psql -U hidrologia -d hidrologia \
  -c "select * from bi_status_backfill order by atualizado_em desc limit 20;"
```

## 9. Migrações

`app/migrate.py` aplica os arquivos de `migrations/` uma única vez (registro em
`schema_migrations`) e **reaplica** `sql/bi_views.sql` a cada start. Para mudar
uma view de BI, basta editar esse arquivo e reiniciar — sem Alembic.

## 10. Próximos passos

- Endpoint e medida DAX de tendência (1h/3h/6h/12h/24h).
- Régua de alerta por estação (atenção / alerta / inundação) — depende das
  cotas de referência da Defesa Civil, que não vêm da ANA.
- Métricas Prometheus e alarme de coleta parada.
- Backup do PostgreSQL (`pg_dump` agendado).
- JWT ou mTLS na API institucional.
