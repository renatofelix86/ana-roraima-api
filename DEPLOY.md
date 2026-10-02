# Deploy e atualização na VPS

Passo a passo para colocar em produção e, depois, para atualizar o código sem
perder dados.

---

## 1. Deploy inicial

Feito uma única vez, na VPS.

### 1.1 Pré-requisitos

- Docker e Docker Compose na VPS
- O arquivo de backup gerado na máquina local (`./scripts/backup.sh`)

### 1.2 Enviar o backup

O dump **não vai no git** (`backups/` está no `.gitignore`), então transfira à
parte. E como git não versiona pasta vazia, `backups/` **não existe** depois do
clone — crie antes de enviar:

```bash
ssh USUARIO@SERVIDOR "mkdir -p /caminho/ana-roraima-api/backups"

scp backups/hidrologia-AAAAMMDD-HHMMSS.dump USUARIO@SERVIDOR:/caminho/ana-roraima-api/backups/
```

> **No PowerShell**, mantenha cada comando em **uma linha só**. A barra
> invertida `\` de continuação é sintaxe do Bash; no PowerShell ela vira um
> argumento e o `scp` tenta copiar para a raiz `\`, falhando com
> "Acesso negado". Para quebrar linha no PowerShell o caractere é a crase
> `` ` ``. No Git Bash, o `\` funciona normalmente.

Confira a integridade depois de transferir — é arquivo binário, e uma
transferência truncada não dá erro visível, só falha na hora de restaurar:

```bash
# na sua máquina
sha256sum backups/hidrologia-AAAAMMDD-HHMMSS.dump

# na VPS
ssh USUARIO@SERVIDOR "sha256sum /caminho/ana-roraima-api/backups/hidrologia-AAAAMMDD-HHMMSS.dump"
```

Os dois valores têm que ser idênticos.

**Não commite o dump no git.** Ele não tem credenciais (o `pg_dump` roda com
`--no-owner --no-privileges`), então não é risco de segurança — o problema é
que o git guarda tudo para sempre: 18 MB de binário ficam no histórico mesmo
depois de apagados, e todo clone futuro passa a baixá-los. Desfazer exigiria
reescrever o histórico com `git filter-repo` e `push --force`. Se precisar
mesmo de um caminho via git, anexe como *asset de um Release* — isso fica fora
do histórico do repositório.

### 1.3 Na VPS

```bash
git clone <seu-repo> ana-roraima-api
cd ana-roraima-api

# 1. o .env NAO vem no git -- crie a partir do exemplo
cp .env.example .env
nano .env
```

Revise no `.env`:

| Variável | Atenção |
|---|---|
| `ANA_IDENTIFIER` / `ANA_PASSWORD` | credenciais da ANA |
| `POSTGRES_PASSWORD` | senha forte, diferente da local |
| `DATABASE_URL` | precisa conter a **mesma** senha acima |
| `APP_API_KEY` | chave forte |
| `BI_READONLY_PASSWORD` | senha do usuário do Power BI |
| `API_HOST_PORT` | troque se 8000 já estiver em uso na VPS |
| `DB_HOST_PORT` | troque se 5433 já estiver em uso na VPS |

```bash
# 2. sobe SO o banco
docker compose up -d db

# 3. restaura o backup
# (o dump já foi enviado para backups/ na etapa 1.2)
./scripts/restore.sh backups/hidrologia-AAAAMMDD-HHMMSS.dump

# 4. sobe api e worker
docker compose up -d

# 5. recria o usuario somente-leitura do Power BI
docker compose run --rm api python -m app.bi_user

# 6. confere
curl http://127.0.0.1:${API_HOST_PORT:-8000}/health
docker compose logs -f worker
```

**Por que subir só o banco no passo 2:** com o worker de pé, ele cria as
tabelas e começa a coletar no meio da restauração.

**O que a restauração economiza:** além dos dados, evita 3.234 requisições à
ANA. Como `backfill_progress` vem no dump, o worker entende que as 69 estações
já estão completas e faz apenas a atualização diária leve.

---

## 2. Atualizar o código em produção

O procedimento que você vai repetir a cada mudança.

### 2.1 Receita padrão

```bash
cd /caminho/ana-roraima-api

./scripts/backup.sh          # 1. SEMPRE antes de atualizar
git pull                     # 2. traz o código novo
docker compose build         # 3. reconstrói as imagens
docker compose up -d         # 4. recria os containers com a imagem nova

docker compose logs -f worker   # 5. acompanha a subida
```

Depois confira:

```bash
curl http://127.0.0.1:8000/health
```

### 2.2 A pegadinha do `restart`

```bash
docker compose restart      # NAO pega imagem nova -- so reinicia o processo
docker compose up -d        # <- este e o certo apos um build
```

`restart` reinicia o container **existente**, com a imagem antiga. Depois de um
`build`, sempre `up -d`: o Compose percebe que a imagem mudou e recria o
container.

E o contrário também vale: se nada mudou no código, `up -d` responde
`Container ... Running` e **não recria nada**. Não é falha — é o Compose
vendo que a imagem é a mesma. Para forçar mesmo assim:

```bash
docker compose up -d --force-recreate
```

### 2.3 O que exige rebuild

`app/`, `sql/` e `migrations/` são **copiados para dentro da imagem** (ver o
`Dockerfile`). Editar qualquer um deles na VPS não tem efeito nenhum até
reconstruir.

| Você mudou | Precisa |
|---|---|
| `app/*.py` | `build` + `up -d` |
| `sql/bi_views.sql` (views do Power BI) | `build` + `up -d` |
| `migrations/*.sql` | `build` + `up -d` |
| `.env` (senhas, portas, intervalos) | só `up -d` |
| `docker-compose.yml` | só `up -d` |

### 2.4 O que acontece sozinho na subida

Ao iniciar, `api` e `worker` chamam `init_db()`, que:

1. cria tabelas que ainda não existam (`create_all`);
2. aplica as migrações de `migrations/` que ainda não rodaram, registrando-as
   em `schema_migrations` — cada arquivo roda **uma vez só**;
3. derruba e recria todas as views `bi_*`.

Ou seja: **você não roda migração à mão**. Para conferir que passou:

```bash
docker compose logs worker | grep -E "Aplicando migração|Views de BI"

docker compose exec db psql -U hidrologia -d hidrologia \
  -c "select * from schema_migrations order by applied_at;"
```

### 2.5 Impacto durante a atualização

- **worker**: reiniciar é seguro a qualquer momento. Os jobs são idempotentes
  (UPSERT) e a carga histórica tem checkpoint por estação — ela retoma de onde
  parou. Uma coleta interrompida é refeita no ciclo seguinte, e a janela
  `HORA_24` recupera o que ficou para trás.
- **api**: fica alguns segundos fora do ar enquanto o container é recriado.
- **Power BI / pgAdmin**: as views são derrubadas e recriadas na subida. Um
  refresh que caia exatamente nesse instante pode falhar; basta repetir.

---

## 3. Rollback

```bash
git log --oneline -5
git checkout <commit-anterior>
docker compose build && docker compose up -d
```

**Importante: migração não volta sozinha.** As migrações são só de ida — voltar
o código não desfaz uma mudança de schema já aplicada. Se a versão nova alterou
o banco e você precisa realmente reverter o schema:

```bash
git checkout <commit-anterior>
docker compose up -d db
./scripts/restore.sh backups/<backup-de-antes-da-atualizacao>.dump
docker compose up -d
```

É por isso que o passo 1 da receita é o backup.

---

## 4. Comandos perigosos

| Comando | Efeito |
|---|---|
| `docker compose down -v` | **APAGA O VOLUME — todos os dados somem.** |
| `docker compose down` | Para e remove os containers. O volume fica. Seguro. |
| `docker volume rm ana-roraima-api_postgres_data` | Apaga o banco inteiro. |

O `-v` é a diferença entre parar a aplicação e perder 59 anos de série
histórica. Na dúvida, `docker compose stop`.

Outro ponto de atenção: `sql/00_drop_views.sql` usa `DROP VIEW ... CASCADE`. Se
alguém criar no banco um objeto que dependa de uma view `bi_*`, ele será
derrubado junto na próxima subida. Construa em cima das views a partir do Power
BI, não criando objetos dependentes no Postgres.

---

## 5. Verificação pós-deploy

```bash
# containers de pé
docker compose ps

# API viva e dado fresco
curl http://127.0.0.1:8000/health

# migrações aplicadas
docker compose exec db psql -U hidrologia -d hidrologia \
  -c "select * from schema_migrations order by applied_at;"

# coleta funcionando (espere até 15 min pelo primeiro ciclo)
docker compose exec db psql -U hidrologia -d hidrologia \
  -c "select * from bi_status_coleta order by iniciado_em desc limit 5;"

# volume de dados
docker compose exec db psql -U hidrologia -d hidrologia \
  -c "select count(*) from measurements;"
```

O `/health` é o teste mais rápido: devolve a idade da última medição. Lembre
que a ANA publica com atraso próprio de até ~45 min, então `idade_minutos`
abaixo de 60 é normal.

---


---

## 6. Publicar a API pelo Traefik (opcional)

A VPS já roda Traefik v2.10 com entrypoints `web` (:80) e `websecure` (:443) e
o certresolver `myresolver`. Em vez de expor a porta 8000, dá para servir a API
por domínio com HTTPS automático.

### 6.1 Antes de subir

1. **DNS.** O subdomínio já está criado na Cloudflare e resolve para
   `200.164.65.190` (verificado em 2026-09-09 pelos resolvedores 1.1.1.1 e
   8.8.8.8). O Let's Encrypt usa desafio HTTP: o nome precisa resolver para a
   VPS **antes** de o container subir, senão a emissão falha e o Traefik passa
   a servir a rota sem certificado válido.
2. **Rede do Traefik**: confirmada como `traefik_moodle-net`, que é o valor já
   usado no `docker-compose.traefik.yml`. Para reconferir:
   ```bash
   docker network ls | grep -i traefik
   ```
3. **Cloudflare**: o registro está em DNS only (cinza), confirmado. Nada a
   ajustar — o proxy fora do caminho é o cenário ideal para o Let's Encrypt.
   Detalhes e o que mudaria se um dia ligar o proxy: seção 6.5.

### 6.2 Ativar

No `.env` **da VPS** (não no local), descomente:

```
COMPOSE_FILE=docker-compose.yml:docker-compose.traefik.yml
API_DOMAIN=hidrologia.cbm.rr.gov.br
```

E suba normalmente:

```bash
docker compose up -d
```

Com `COMPOSE_FILE` definido, o Compose junta os dois arquivos sozinho — o
comando é o mesmo de sempre, e o `docker-compose.yml` continua funcionando
puro na máquina local, onde não existe Traefik.

### 6.3 Conferir

```bash
# a API responde por dentro
curl http://127.0.0.1:8000/health

# e pelo domínio, com certificado
curl https://hidrologia.cbm.rr.gov.br/health

# rota registrada no Traefik
docker logs traefik --tail 50 | grep -i hidrologia
```

### 6.4 Detalhes que evitam dor de cabeça

- **`traefik.docker.network` é obrigatória aqui.** O container da API fica em
  duas redes (a do projeto e a do Traefik) e este Traefik subiu sem
  `--providers.docker.network`. Sem a label, ele pode escolher a rede errada e
  a rota falha em silêncio — sem erro no log, só 404.
- **`worker` e `db` levam `traefik.enable=false`.** Este Traefik roda com
  `exposedbydefault` ligado (o padrão), então convém marcar explicitamente o
  que não deve virar rota.
- **O banco não passa pelo Traefik.** Ele roteia HTTP; Postgres não é HTTP.
  O acesso do pgAdmin e do Power BI continua por túnel SSH em
  `127.0.0.1:5433`.
- **A porta 8000 segue publicada no loopback**, o que é útil para `curl` de
  diagnóstico dentro da VPS. Se quiser tirar, remova o bloco `ports:` do
  serviço `api` na sobreposição.
- **Mantenha o `X-API-Key`.** O Traefik dá HTTPS, não autenticação.

### 6.5 Cloudflare

O domínio é gerenciado na Cloudflare. Estado confirmado em 2026-09-09:

**O registro `hidrologia` está em DNS only (nuvem cinza).**

Isso simplifica tudo: com o proxy desligado, a Cloudflare só responde a
consulta de DNS e não entra no caminho do tráfego. O navegador fala direto com
a VPS. Em consequência:

- **O modo SSL/TLS da Cloudflare é irrelevante aqui.** Aquele ajuste
  (Flexible / Full / Full strict) governa apenas como a Cloudflare conversa
  com a origem quando está no meio. Em cinza, ela não está.
- **Não há risco de laço de redirecionamento.** O problema clássico é
  proxied + Flexible; nenhum dos dois se aplica.
- **O Let's Encrypt valida direto contra o Traefik**, que é o cenário mais
  simples e confiável.

#### Se um dia ligar o proxy (laranja)

Aí sim o modo passa a importar. Em **SSL/TLS → Overview** do painel:

| Modo | Efeito |
|---|---|
| Flexible | **Não use.** Cloudflare fala HTTP com a origem; com o nosso redirect HTTP→HTTPS vira laço infinito (`ERR_TOO_MANY_REDIRECTS`). |
| Full | Funciona, mas não valida o certificado da origem. |
| **Full (strict)** | **Use este.** Valida o certificado do Let's Encrypt que o Traefik já emitiu. |

Ligue o laranja só **depois** de o certificado estar emitido e o
`https://hidrologia.cbm.rr.gov.br/health` respondendo.

#### Diagnóstico: o WAF barra o `curl`

Há um firewall na frente da rede que devolve **403** para o User-Agent padrão
do `curl` — inclusive para `cbm.rr.gov.br`. A mesma URL com User-Agent de
navegador responde normalmente.

Um 403 em teste **não** significa serviço fora do ar. Sempre confirme com:

```bash
curl -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 \
(KHTML, like Gecko) Chrome/120.0 Safari/537.36" \
  https://hidrologia.cbm.rr.gov.br/health
```

O agente do Let's Encrypt foi testado e **não** é barrado: o caminho
`/.well-known/acme-challenge/` responde 404 (Traefik sem rota), não 403.


---

## 7. Expor o banco à internet (Power BI com atualização automática)

Necessário quando o Power BI Service precisa atualizar sozinho sem gateway.
Por padrão o banco só escuta em `127.0.0.1`; esta seção troca isso por uma
exposição endurecida.

### 7.1 O que se protege, e por quê

O dado hidrológico é público — o risco não é ele, é o servidor. Duas
descobertas moldaram o desenho:

**O IP de origem é mascarado.** Publicada pelo Docker, uma conexão externa
chega ao Postgres como `172.18.0.1`, o gateway da bridge, e não como o IP real
do cliente. Regras por faixa de IP no `pg_hba.conf` não separariam "container
da aplicação" de "internet" — um atacante cairia na regra permissiva. Por isso
a restrição aqui é por **papel e TLS**, nunca por IP.

**O superusuário não pode ser rebaixado.** `hidrologia` inicializou o cluster,
e o PostgreSQL recusa `ALTER ROLE ... NOSUPERUSER` no usuário de bootstrap
(*"The bootstrap user must have the SUPERUSER attribute"*). Como superusuário
significa execução de comando no servidor (`COPY FROM PROGRAM`), a saída é não
expor esse papel: `app.harden_db` cria `hidro_app`, um papel comum que passa a
ser dono dos objetos e a rodar a aplicação.

Resultado verificado:

| Papel | Alcance | Poder |
|---|---|---|
| `hidrologia` | **só socket local** | superusuário, administração e emergência |
| `hidro_app` | TCP com TLS | dono dos objetos; `COPY TO PROGRAM` negado |
| `powerbi_ro` | TCP com TLS | `SELECT` só nas views `bi_*` |

### 7.2 Ordem — não pode ser trocada

O `pg_hba` endurecido só aceita `hidro_app`, que ainda não existe quando você
sobe pela primeira vez. Ativar a sobreposição antes de criar o papel tranca
você para fora. Portanto:

```bash
# 1. certificado TLS (uma vez)
./scripts/setup-tls.sh hidrologia.cbm.rr.gov.br

# 2. banco no modo PADRÃO ainda (sem a sobreposição), para poder criar o papel
docker compose up -d db
./scripts/restore.sh backups/<arquivo>.dump

# 3. cria hidro_app e transfere a posse dos objetos
docker compose run --rm api python -m app.harden_db

# 4. aponte o DATABASE_URL para o papel novo, no .env:
#      DATABASE_URL=postgresql+psycopg://hidro_app:<PG_APP_PASSWORD>@db:5432/hidrologia

# 5. AGORA sim, ative a exposição no .env:
#      COMPOSE_FILE=docker-compose.yml:docker-compose.traefik.yml:docker-compose.exposed.yml
#      DB_BIND_IP=0.0.0.0
docker compose up -d

# 6. usuário de leitura do Power BI
docker compose run --rm api python -m app.bi_user

# 7. libere a porta no firewall da VPS
sudo ufw allow 5433/tcp
```

No Linux o separador do `COMPOSE_FILE` é `:`; no Windows seria `;`.

### 7.3 Conferir

```bash
# TLS ativo
docker compose exec db psql -U hidrologia -d hidrologia -c "show ssl;"

# regras em vigor
docker compose exec db psql -U hidrologia -d hidrologia \
  -c "select type, database, user_name, auth_method from pg_hba_file_rules where error is null;"
```

Esperado: `local/trust`, `hostssl/hidro_app`, `hostssl/powerbi_ro` — e nada mais.

De outra máquina, o superusuário tem que ser recusado e os dois papéis comuns
têm que entrar:

```bash
psql "host=hidrologia.cbm.rr.gov.br port=5433 dbname=hidrologia user=hidrologia sslmode=require"   # RECUSADO
psql "host=hidrologia.cbm.rr.gov.br port=5433 dbname=hidrologia user=powerbi_ro sslmode=require"   # conecta
```

### 7.4 Manutenção depois de endurecido

`hidrologia` deixa de conectar por TCP — inclusive de dentro dos containers.
Administração passa a ser pelo socket local:

```bash
docker compose exec db psql -U hidrologia -d hidrologia
```

Foi assim que resolvi, por exemplo, a concessão de ADMIN sobre o `powerbi_ro`
ao `hidro_app` (necessária no PostgreSQL 16 quando o papel foi criado por
outro). Em instalação nova isso não ocorre: o `powerbi_ro` passa a ser criado
pelo próprio `hidro_app`, que já nasce com essa permissão.

**Editar `postgres/pg_hba.conf` não tem efeito imediato** — o Postgres não
recarrega sozinho:

```bash
docker compose exec db psql -U hidrologia -d hidrologia -c "select pg_reload_conf();"
```

### 7.5 Certificado reconhecido (obrigatório para o Power BI)

O certificado autoassinado de `setup-tls.sh` cifra o tráfego, mas **o Power BI
recusa conectar com ele**. O conector de PostgreSQL do Power BI (driver
Npgsql) valida a cadeia pelo repositório de certificados do Windows, e não
oferece opção de ignorar. Verificado em 2026-09-10 da máquina de
desenvolvimento:

| Porta | Certificado | Validação do Windows |
|---|---|---|
| 5433 (Postgres) | autoassinado | **recusado** — "raiz não confiável" |
| 443 (Traefik) | Let's Encrypt | aprovado — `YR1 → Root YR → ISRG Root X1` |

A saída de emergência habitual do Power BI, "conectar sem criptografia", não
existe aqui: o `pg_hba.conf` recusa conexão sem TLS.

A solução é o Postgres usar **o mesmo certificado Let's Encrypt que o Traefik
já emite e renova** para `hidrologia.cbm.rr.gov.br`:

```bash
./scripts/sync-cert.sh
```

Ele localiza o `acme.json` pelo volume do container `traefik`, extrai o
certificado do domínio, **valida que certificado e chave abrem e são do mesmo
par**, e recarrega o Postgres a quente — as conexões abertas não caem. Se
algo estiver errado, aborta sem alterar nenhum arquivo.

O Traefik renova o certificado a cada ~60 dias. Agende o script diariamente;
ele só age quando o arquivo realmente mudou:

```cron
30 4 * * * cd /home/cbmrr/docker/ana-roraima-api && bash scripts/sync-cert.sh >> /var/log/ana-sync-cert.log 2>&1
```

Sem o cron, o Power BI para de conectar quando o certificado vencer (o atual
vence em 08/12/2026).

O `setup-tls.sh` continua útil só para a **primeira** subida, antes de o
Traefik ter emitido o certificado do domínio.

### 7.6 Power BI e pgAdmin

| Campo | Valor |
|---|---|
| Servidor | `hidrologia.cbm.rr.gov.br:5433` |
| Banco | `hidrologia` |
| Usuário | `powerbi_ro` (relatórios) ou `hidro_app` (administração) |
| Criptografia | obrigatória — conexão sem TLS é recusada |

No pgAdmin, `sslmode=require` basta.

## 8. Se algo der errado

**Container reiniciando em loop** — quase sempre migração que falhou ou
`DATABASE_URL` errada:

```bash
docker compose logs --tail 50 worker
docker compose logs --tail 50 api
```

**`401` repetido no log da ANA** — credenciais erradas no `.env`. Teste
isolado:

```bash
docker compose run --rm api python -m app.check_ana
```

**Coleta sem gravar nada** — veja se o inventário rodou e se há estações
telemétricas:

```bash
docker compose exec db psql -U hidrologia -d hidrologia \
  -c "select count(*) from stations where telemetered and operating;"
```

**Power BI não conecta** — o banco só escuta em `127.0.0.1`. De fora, use túnel
SSH (seção 7.4 do README):

```bash
ssh -L 5433:127.0.0.1:5433 usuario@vps
```

**Porta já em uso ao subir** — outra aplicação da VPS ocupa a porta. Ajuste
`API_HOST_PORT` ou `DB_HOST_PORT` no `.env` e rode `docker compose up -d`.
