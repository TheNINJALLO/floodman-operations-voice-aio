# Roomflow Integration

## Integration model

The system does not hard-code private Roomflow routes. It uses named operations mapped in `floodman.yaml`:

```yaml
roomflow:
  endpoints:
    lookup_customer:
      method: GET
      path: /api/v1/your-route
      query_keys: [phone, name, address]
```

The base URL and credentials are environment variables:

```dotenv
ROOMFLOW_ENABLED=true
ROOMFLOW_BASE_URL=https://your-roomflow-host
ROOMFLOW_TOKEN=<bearer token>
ROOMFLOW_COMPANY_ID=<company ID>
```

## Supported operations

- `lookup_customer`
- `create_lead`
- `create_emergency_case`
- `check_availability`
- `schedule_inspection`
- `reschedule_inspection`
- `verify_customer_identity`
- `get_billing_summary`
- `send_payment_link`
- `create_callback_task`
- `send_photo_upload_link`
- `record_upload`
- `record_call_outcome`
- `record_opt_out`
- `record_security_event`

Use `config/examples/roomflow-endpoints.example.yaml` to build the exact map.

## Request authentication

Every configured request includes:

```http
Authorization: Bearer <ROOMFLOW_TOKEN>
X-COMPANY-ID: <ROOMFLOW_COMPANY_ID>
Idempotency-Key: <stable generated key>
User-Agent: Floodman-Operations-Voice-AIO/1.1.1
```

Additional headers can be defined per operation.

## Body shaping

By default, the complete payload is sent. A mapping can select and rename fields:

```yaml
create_lead:
  method: POST
  path: /api/v1/leads
  body_map:
    customerName: name
    phoneNumber: phone
    serviceAddress: address
    source: source
    voiceCallId: call_id
```

Nested source fields use dotted paths.

## Local-first behavior

The local ledger stores business data before Roomflow is called. This guarantees an operator can recover the caller’s information even when the remote API is unavailable.

Mutating Roomflow operations use a durable outbox. The dashboard shows pending, retrying, and completed synchronization records. Replays keep the original idempotency key.

## Customer ingestion

The lookup adapter understands common response shapes:

```json
{"customer": {...}}
```

```json
{"customers": [{...}]}
```

```json
{"results": [{...}]}
```

Customer and property records returned by Roomflow are cached into the local ledger. Adjust `_ingest_remote_customer` if the private response uses different field names.

## Recommended Roomflow fields

Store these call fields when possible:

- Asterisk unique ID
- Floodman call ID
- direction
- agent slug
- provider
- source classification
- Google announcement detected
- transcript and recording references
- summary and disposition
- campaign ID
- consent record ID
- identity verification result
- appointment or invoice references
- opt-out category
- final revenue attribution

## Validation

Before enabling production synchronization:

1. Use a Roomflow staging company.
2. Map one read-only customer lookup.
3. Map lead creation and verify idempotency.
4. Disable Roomflow and confirm the outbox captures the same request.
5. Re-enable Roomflow and replay the outbox.
6. Map scheduling only after date, timezone, and duplicate behavior are verified.
7. Map billing only after the identity-verification boundary is tested.
