---
name: meta-performance-analyzer
description: "Analisa performance das campanhas Meta usando o decide() personalizado do aluno (limiares scale_at/kill_at do perfil) e retorna ranking de ações: top 3 SCALE, top 3 KILL, top KEEP a monitorar. Lê paid-traffic-{N}d.json e meta_perfil.json. Use SEMPRE que o aluno disser: analisar campanhas meta, performance da semana, qual ad escalar, qual matar, relatorio meta, analise meta, analise trafego pago, performance meta ads."
model: sonnet
effort: high
---

# Meta Performance Analyzer

Diagnóstico semanal de tráfego pago Meta com decisões prontas.

## Inputs

1. Pergunte ao aluno qual janela analisar (default 7d). Se ele disser "essa semana" use 7d, "mês" use 30d, etc.
2. Leia:
   - `~/.operacao-ia/dashboards/paid-traffic-{N}d.json`
   - `~/.operacao-ia/config/meta_perfil.json`

Se o JSON não existe, oriente a rodar `meta-metrics-fetcher` primeiro.

O JSON de cada janela também traz, por conta (dentro de `accounts[]`):
- `recent_activities`: log de mudanças recentes na conta (orçamento, público, criativo, status) dentro da janela — já filtrado pelo período.
- `pixel_health`: status do(s) pixel(s) vinculados (`HEALTHY` / `DEGRADED` / `UNHEALTHY` / `SEM_PIXEL`), com `hours_since_last_fire` por pixel.

E no nível raiz do JSON:
- `period_over_period`: comparação do KPI primário e demais KPIs do perfil entre a janela atual e a janela equivalente imediatamente anterior (`prev_kpis_summary`, mesma estrutura de `kpis_summary`). Quando `available` é `false`, o campo `reason` explica por quê (ex: janela de 30d não tem histórico suficiente coletado pra comparar) — nesse caso não invente uma tendência, diga que não dá pra comparar essa janela.

## Análise

1. Para cada ad no JSON, leia `decide` e `decide_reason` (já calculado pelo fetcher)
2. Agrupe por decisão: SCALE / KILL / KEEP / VALIDATION
3. Ordene cada grupo por `spend` desc (alta prioridade primeiro)
4. Compare KPI primário vs `target` por ad — calcule % delta
5. Identifique padrões: "todos os ads de público lookalike-X estão SCALE", "campanhas Reels superam Feed em CTR", etc.
6. Antes de confiar em qualquer veredito de custo, cheque `accounts[].pixel_health`. Se `overall_status` for `DEGRADED` ou `UNHEALTHY` pra alguma conta, isso é suspeito primário — sinalize isso ANTES de qualquer outra causa, porque dado de conversão com tracking degradado não é confiável (nem pra SCALE nem pra KILL).
7. Cheque `accounts[].recent_activities`. Se uma campanha/conjunto em SCALE ou KILL teve uma edição de orçamento/público/criativo/status dentro da própria janela analisada, cite isso explicitamente como possível causa — não deixe o veredito passar como "mercado" ou "audiência" sem checar se foi uma mudança manual.
8. Use `period_over_period` (quando `available: true`) pra dizer se o KPI primário está piorando, estável ou melhorando frente ao período anterior equivalente — não só o valor absoluto da janela atual. Quando `available: false`, diga isso explicitamente em vez de omitir a comparação.

### Ads/campanhas marcados como VALIDATION
Campanhas cujo nome bate com `decide_exclude_tags` do perfil (ex: campanhas `|ENG|` de teste de audiência/validação de conteúdo) não recebem veredito de SCALE/KILL pelo KPI primário — não é o objetivo delas. Trate-as numa seção própria, ranqueadas por CTR (não por cost_per_msg/CPL/etc), e não misture com o bloco KEEP nem com a leitura do KPI primário da conta.

### Múltiplas contas de anúncio (perfil com `ad_account_ids`)
Quando o perfil tem mais de uma conta configurada, o JSON traz um array `accounts` (id + nome + status) e cada item em `campaigns` vem com `account_id`/`account_name`. Nesse caso, identifique cada campanha pelo nome da conta (não só pelo ID) em todas as tabelas — inclusive nos blocos SCALE/KILL/KEEP/VALIDATION — para não confundir campanhas de contas diferentes. As metas de KPI (`target`, `decide_exclude_tags`) continuam globais do perfil, aplicadas igualmente a todas as contas.

### Mudanças recentes na conta (`recent_activities`)
Cada item tem `event_type`, `translated_event_type`, `object_name`, `object_type`, `event_time` e `extra_data` (valores antigo/novo quando disponível). Cruze com o objeto afetado (`object_name`) pra ver se bate com uma campanha/conjunto que está em SCALE/KILL — é a mesma lógica da causa #4 do framework de diagnóstico (mudanças recentes na conta antes de culpar mercado/audiência).

### Saúde do pixel/tracking (`pixel_health`)
`overall_status` é o pior status entre os pixels da conta. `SEM_PIXEL` não é necessariamente um problema (a campanha pode não depender de pixel), mas `DEGRADED`/`UNHEALTHY` numa conta que otimiza por mensagem/conversão pixel-based é motivo pra desconfiar do dado antes de recomendar qualquer ação de orçamento.

### Comparativo com período anterior (`period_over_period`)
Estrutura: `{available, prev_since, prev_until, prev_spend_total, prev_kpis_summary}` (ou `{available: false, reason}`). Compare `kpis_summary[key].value` (atual) com `period_over_period.prev_kpis_summary[key].value` (anterior) pra calcular a variação — não existe `delta_pct` pronto entre os dois, calcule a partir dos dois valores.

## Output (markdown)

```markdown
# 📊 Análise Meta ADS — janela {N}d

**Conta:** act_xxxxx
**Período:** {since} → {until}
**Spend total:** R${spend_total}
**KPI primário:** {primary_kpi} (meta R${target})

## Status global do KPI primário
{primary_kpi}: R${value} vs meta R${target} — {🟢 / 🟡 / 🔴} ({delta_pct}%)

---

## 🕐 Tendência vs período anterior
{se period_over_period.available: "{KPI primário} {melhorou/piorou/estável}: R${prev_value} → R${current_value} ({delta_pct}%)", período {prev_since} → {prev_until}}
{se available=false: "Comparação de período indisponível pra essa janela: {reason}"}

## 🛠️ Mudanças recentes na conta
{lista de recent_activities relevantes no período, com data + o que mudou; "Nenhuma mudança registrada no período" se vazio}

## 📡 Saúde do pixel/tracking
{pixel_health.overall_status por conta; se DEGRADED/UNHEALTHY, alerta em destaque antes dos blocos SCALE/KILL}

---

## 🟢 TOP 3 SCALE (escalar agora)

| Ad | Spend | {KPI} | vs meta | Razão |
|---|---|---|---|---|
| {ad_name 1} | R${spend} | R${kpi} | -25% | CPL bem abaixo + amostra |
| ... | ... | ... | ... | ... |

## 🔴 TOP 3 KILL (pausar)

| Ad | Spend | {KPI} | vs meta | Razão |
|---|---|---|---|---|
| {ad_name 1} | R${spend} | R${kpi} | +85% | CPL muito acima sem indício de melhora |
| ... | ... | ... | ... | ... |

## 🟡 KEEP (manter monitorando)

{lista resumida — 5-10 ads}

## 🧪 VALIDATION (teste de conteúdo/audiência — sem veredito de {KPI primário})

| Ad | Spend | CTR | Impressões | Obs |
|---|---|---|---|---|
| {ad_name 1} | R${spend} | {ctr}% | {impressions} | melhor CTR do teste |
| ... | ... | ... | ... | ... |

---

## Padrões observados

- {insight 1: ex. "Lookalike 1% supera Lookalike 3% em CPL"}
- {insight 2: ex. "Criativo vídeo tem CTR 40% maior que estático"}
- {insight 3: ex. "Sexta-feira tem CPL 30% mais alto — considerar reduzir budget"}

---

## Próximas ações sugeridas

1. Pausar os 3 ads do bloco KILL → economia estimada de R${spend_kill}/dia
2. Aumentar budget dos 3 SCALE em 30% → testar se mantém CPL
3. Replicar padrão {insight} em novas campanhas
```

## Enviar a análise pro grupo do WhatsApp

Se o aluno pedir pra mandar a análise pro grupo (ex: "manda isso no grupo", "dispara pro WhatsApp", "manda a análise da conta X lá"):

1. Gere a análise normal (seção Output acima) e mostre na tela primeiro — nunca pule direto pro envio sem o aluno ver o conteúdo completo antes.
2. Componha uma versão CURTA e sem tabela markdown pra WhatsApp (negrito com `*asterisco*`, sem `|` de tabela, sem `#` de título) contendo, nessa ordem: conta analisada + janela, tendência do KPI primário (via `period_over_period`, se disponível), alerta de pixel em destaque se `pixel_health.overall_status` for `DEGRADED`/`UNHEALTHY`, até 2 SCALE e até 2 KILL com o número que embasa cada um, causa provável (se identificada, citando o dado) e a recomendação. O envio real corta em 1200 caracteres (mesmo limite testado do relatório diário) — priorize o que for mais acionável se precisar cortar.
3. Salve essa versão curta num arquivo temporário e rode primeiro, sempre com `--dry-run`:
   ```bash
   python3 ~/.operacao-ia/scripts/meta/send_whatsapp_analysis.py --message-file <arquivo> --dry-run
   ```
4. Mostre o resultado do dry-run (destino mascarado + texto exato) pro aluno e só rode sem `--dry-run` depois que ele confirmar.
5. Se a config do WhatsApp (`~/.operacao-ia/config/meta.env`) estiver incompleta, oriente a rodar o setup da skill `meta-relatorio-whatsapp` primeiro — essa skill aqui não configura o provider, só reaproveita a config já feita.

## Regras

- Nunca recomende ação sem mostrar o número
- Se decide() está desabilitado no perfil (`decide_enabled: false`), faça análise descritiva sem dar veredito
- Se `spend < target × 1.2` para um ad, marque como "amostra insuficiente" — não classifique
- Inclua sempre o nome real do ad e ID curto (últimos 8 dígitos)
- Se `pixel_health.overall_status` for `DEGRADED` ou `UNHEALTHY`, avise isso no topo do relatório, antes do ranking SCALE/KILL — não deixe essa informação só no rodapé
- Nunca afirme que uma métrica "está piorando" ou "está melhorando" sem citar o número de `period_over_period`; se `available: false`, diga que a comparação não estava disponível em vez de estimar
- Nunca envie a análise pro grupo do WhatsApp sem antes mostrar a mensagem final (via `--dry-run`) e ter confirmação explícita do aluno
