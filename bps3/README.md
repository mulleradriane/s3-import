# Changelog :newspaper:

# 2.2.1 [unreleased]

## Patch

- **Lifecycle — alinhamento à governança (Asset Category):**
  - **Development / Staging / Sandbox:** corrige regra `30 StandardIA -> 90 Glacier` — passa a emitir transição **90d → GLACIER_IR** (bloco `dynamic` interno usava `rule.value.transition` com `for_each = [1]`, logo a segunda transição nunca era criada e plans de import removiam Glacier na AWS).
  - **Logs / Backup:** segunda transição **90d → GLACIER** (antes 120d, fora do padrão documentado).
  - **Versionamento:** regra `Deleta as 10 versoes...` apenas quando `versioning_configuration = Enabled` **e** `Environment = prd` **e** `Asset_Category` ∈ {Productive data, Embbeded} (antes aplicava também em dev/hml com versioning Suspended/Enabled).
- **Notification (SQS):** policy opcional `KMSAllows` quando `sqs_notifications[<key>].kms_keys` informa lista de ARNs de CMK — evita plan remover permissões KMS em filas SSE-KMS (legado/migração).

# 2.2.0 [08/04/2026]

## Minor

- [PLT-632](https://serasaexperian.atlassian.net/browse/PLT-632) - Adicionando tag group para ser usada como prefixo do nome do bucket.

# 2.1.0 [18/09/2025]

## Minor

- [SRE-21123](https://serasaexperian.atlassian.net/jira/servicedesk/projects/SRE/queues/custom/555/SRE-21123) - Melhoria no módulo S3:

  - Adicionado novo output `bucket_regional_domain_name` para expor o endpoint regional do bucket (ex.: `bucket.s3.us-east-1.amazonaws.com`), recomendado para uso com CloudFront (origem S3 REST).
  - Mantido o output existente `bucket_domain_name` (endpoint global/legado) para compatibilidade.


# 2.0.0 [15/09/2025]
## Major
- [SREK-8269](https://serasaexperian.atlassian.net/browse/SREK-8269) - BP S3 - Atualização da BP de Tags
    - Atualização da BP para usar as Tags em Map, ao invés de variáveis;
    - Exclusão das variáveis que das tags, pois agora estará tudo no Map;
    - Atualização de Referência do módulo Tags para `ref=MAJOR`;
    - Inclusão de nova tag obrigatória 'BusinessServices';

# 1.2.1 [11/04/2025]

## Patch

- [SREK-6284](https://serasaexperian.atlassian.net/browse/SREK-6284) - Melhoria no módulo S3:
  - Adicionado bloco Migration.


# 1.2.0 [26/08/2025]

## Minor

- [SREK-6284](https://serasaexperian.atlassian.net/browse/SREK-6284) - Melhoria no módulo S3:
  - Adicionado condição para desligar Lifecycle de excluir itens em DEV e HML em cenarios que precisamos manter os arquivos
  - Adicionado Função de Custom Metrics com isso é possivel acompanhas 2xx,3xx,4xx,5xx no Datadog.
  - Atualizado Object Ownership Default
  - Corrigido logica de expiração quando utilizar Cache .. para nao criar 2 Lifecycle
  - Atualizado referencia depreciadas
  - Adicionado Migration para S3 legados
  - Removido Count de recursos para reduzir complexidade de count.

# 1.1.0 [12/06/2025]

## Minor

- [SREP-2575](https://serasaexperian.atlassian.net/browse/SREP-2575) - Melhoria no módulo S3:
  - Inclusão obrigatória do bloco `filter { prefix = "" }` em todas as regras de lifecycle, eliminando warnings do provider e garantindo compatibilidade futura.
  - Ajuste das regras para buckets versionados, utilizando corretamente os blocos `noncurrent_version_expiration` e `noncurrent_version_transition`.
  - Melhoria no tratamento de regras customizadas de lifecycle, garantindo sempre a existência de filtro.
  - Implementação do bloco `target_object_key_format` com `partitioned_prefix` para logs, permitindo particionamento por data/hora e facilitando consultas no Athena.
  - Remoção do prefixo `resource.` em todos os `depends_on` e padronização do uso de `[0]` para recursos com `count`.
  - Ajuste das validações e defaults das variáveis para melhor clareza e robustez.
  - Comentários explicativos em pontos críticos do código.


# 1.0.0 [11/04/2025]

## Major

- [PLT-98](https://serasaexperian.atlassian.net/browse/PLT-98) - Primeira versão da blueprint.