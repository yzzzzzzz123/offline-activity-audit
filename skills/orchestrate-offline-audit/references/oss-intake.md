# OSS intake transport contract

Use this reference only for the authenticated OSS-to-audit adapter. It is not an alternative business
pipeline and it never performs scenario analysis inside the HTTP service.

## Lifecycle

1. The trusted upstream calls `POST /api/intake/oss` with exactly `verifyCode`, positive-integer
   `fileId`, and the temporary `downloadUrl`.
2. The service authenticates the request, validates the exact HTTPS download host, derives the stable
   `verifyCode:fileId` idempotency identity, and creates one durable receipt under
   `worktrees/.intake/jobs/`.
3. One background worker downloads the object to
   `<project>/input-oss/<job_id>/<object-basename>.zip`, records response ETag and actual size, computes
   SHA-256, and confirms that it is a ZIP. The complete pre-signed URL remains only in memory and is
   never written to the receipt or logs. Different jobs never share a directory or overwrite a
   same-named archive.
4. The worker launches `skills/orchestrate-offline-audit/scripts/run.py` in a child process with the
   persistent job directory as `--input-dir`. Deep archive safety, routing, evidence extraction, decisions,
   verification, and publication remain in the formal runner.
5. After the formal worktree snapshot is durable, the adapter POSTs exactly `verifyCode`, `fileId`, and
   the deterministic Chinese `result` string to the configured full HTTPS
   `/api/v1/ai/analyze/callback` URL. Callback retries redeliver the saved result only and never rerun
   the formal audit.
6. The verified original ZIP remains in `input-oss` after completion or failure for local traceability.
   Partial or transport-invalid downloads are removed, while extracted trees, model workspaces and the
   transport result file remain temporary. A successful job points to the ordinary local
   `worktrees/<YYYYMMDD>-<producer-model>[-1.N]/` archive.

The child process boundary is required because the CLI runner redirects process-wide stdout/stderr into
the run log; executing it in an HTTP handler thread would mix concurrent access logs into the audit log.
OSS jobs are serialized to avoid competing long-running visual-model jobs. Manual CLI runs still use the
same worktree reservation and no-overwrite behavior.

## Enablement

The read-only workbench remains the default. Enable intake explicitly with
`--enable-oss-intake` or `OFFLINE_AUDIT_OSS_ENABLED=1` and provide:

- `OFFLINE_AUDIT_OSS_WEBHOOK_SECRET`: at least 16 characters; accepted through
  `Authorization: Bearer ...` or `X-Offline-Audit-Token`;
- one or more exact download hosts through repeated `--oss-allowed-host` or comma-separated
  `OFFLINE_AUDIT_OSS_ALLOWED_HOSTS`;
- the full HTTPS callback URL through `--oss-callback-url` or
  `OFFLINE_AUDIT_OSS_CALLBACK_URL`; its path must be `/api/v1/ai/analyze/callback`;
- optional callback Bearer credentials through `OFFLINE_AUDIT_OSS_CALLBACK_TOKEN`;
- optional `OFFLINE_AUDIT_OSS_PRODUCER_MODEL`, `OFFLINE_AUDIT_OSS_MAX_BYTES`,
  `OFFLINE_AUDIT_OSS_DOWNLOAD_TIMEOUT`, `OFFLINE_AUDIT_OSS_CALLBACK_TIMEOUT`,
  `OFFLINE_AUDIT_OSS_CALLBACK_ATTEMPTS`, and `OFFLINE_AUDIT_OSS_ALLOW_PRIVATE_HOSTS`.

Do not expose the built-in HTTP listener directly to the public internet. Put HTTPS API gateway,
reverse-proxy, VPN, or a cloud function in front of it for cross-network callbacks. Private-address OSS
resolution is rejected unless explicitly enabled for an exact allowlisted private endpoint.

## Request

`POST /api/intake/oss` accepts one UTF-8 `application/json` object no larger than 64 KiB:

```json
{
  "verifyCode": "HX202603250014",
  "fileId": 123,
  "downloadUrl": "https://exact-allowed-host/path/维护费用.zip?provider-signature=..."
}
```

- All three fields are required. `verifyCode` must be a safe nonempty ASCII identifier and `fileId`
  must be a 64-bit positive integer.
- The URL path or response `Content-Disposition` should retain a Windows-safe `.zip` basename with the
  business scenario marker needed by the formal classifier.
- The first valid `YYYYMMDD` inside `verifyCode` becomes the run business date; otherwise use the current
  Shanghai date.
- Only HTTPS port 443, an exact configured hostname, safe redirects, and public DNS results are accepted
  by default. Userinfo, URL fragments, arbitrary hosts, and private/link-local/loopback resolution fail
  before download.

The `verifyCode:fileId` pair and URL identity without query credentials form the idempotency fingerprint.
Replaying the same pair and object path with a refreshed signature returns the same job; reusing the pair
for a different object path returns `409 Conflict`.

## Result callback

After formal completion, send UTF-8 JSON to the configured callback URL:

```json
{
  "verifyCode": "HX202603250014",
  "fileId": 123,
  "result": "核销分析完成，共1个场景，发现2个错误项。维护费用核销：当前结论：资料需补正。"
}
```

The callback uses `Idempotency-Key: <verifyCode>:<fileId>`, accepts any `2xx`, and may retry network
errors, `408`, `429`, and `5xx`. It never includes `downloadUrl`, local absolute paths, webhook secrets,
or callback credentials. A callback failure is `callback_failed`; retain the completed result and never
rerun AI merely to redeliver it.

## Response and status

A newly accepted job returns `202 Accepted` with a safe job projection and relative `status_url`.
Poll `GET /api/intake/jobs/<24-hex-job-id>` with the same authentication. States are:

`accepted → downloading → running → callback → completed | failed`

A completed job includes the formal runner receipt and a delivered callback receipt. A failed job includes
a bounded, URL-scrubbed message and, if the formal runner had already completed before callback failure,
the retained completed result. Job status is operational metadata and must not be represented as business
evidence. Once download verification succeeds, `delivery` also contains the local persistent
`input_directory` and `input_file` under `input-oss`.

If the service restarts before completion, active receipts are marked failed because the signed URL is
intentionally not persisted. Never silently replay a potentially billable AI run after restart. Inspect the
terminal receipt; when a deliberate new formal analysis is required, the upstream must issue a new
`fileId` and fresh sufficiently long-lived URL. A previously verified ZIP may remain in its original job
directory for diagnosis, while the new `verifyCode:fileId` pair receives a new job directory.
