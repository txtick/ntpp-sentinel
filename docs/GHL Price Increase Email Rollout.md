# GHL Price Increase Email Rollout

This is the agreed process for sending the customer price increase email through GoHighLevel without triggering the full rollout before everything is staged and verified.

## Goal

Use Sentinel to calculate each customer's new service rate, write that value into GHL on the matching contact's existing `monthly_rate` field, and only then trigger a GHL workflow that sends the ready-made email template.

## GHL Setup Order

1. Create the customer custom field in GHL.
2. Build and verify the email template.
3. Build the workflow that sends the email when a tag is added.
4. Keep the workflow inactive or pointed at a test tag until validation is complete.
5. After the data push is verified on a few contacts, apply the real send tag in small batches and then to the full cohort.

## Required GHL Objects

### 1. Contact custom field

- Existing field to use: `monthly_rate`
- Field type: monetary / currency
- Record type: contact
- Group: `Contact`

Why `Contact`:

- The value belongs to the person/customer record.
- The workflow email will merge from the contact record.
- We are updating matched GHL contacts, not opportunities or companies.

### 2. Email template

The email template should use the `monthly_rate` merge field and be fully finalized before any send tag is applied.

### 3. Workflow

Recommended workflow shape:

- Trigger: tag added
- Test trigger tag: something like `sri-042026-send-test`
- Production trigger tag: something like `sri-042026-send`
- Action: send the prepared email template

Keep the workflow disabled, or only pointed at the test tag, until the contact field population has been verified.

## Sentinel / Script Plan

The automation script should do this:

1. Read the Skimmer pricing export.
2. Match each Skimmer customer to the correct existing GHL contact.
3. Update the GHL contact custom field `monthly_rate`.
4. Optionally apply a GHL tag that triggers the workflow.

We do not need additional custom fields unless the email later needs them. Right now the only required merge value is `monthly_rate`.

## Safe Rollout Plan

### Phase 1: Prepare

- Confirm the GHL custom field exists.
- Confirm the email template renders the `monthly_rate` merge field.
- Confirm the workflow trigger tag name.
- Keep sends disabled until a small test batch is ready.

### Phase 2: Test batch

- Push `monthly_rate` to 3-5 known test contacts.
- Apply the test trigger tag to only those contacts.
- Verify:
  - the right contacts were matched
  - the `monthly_rate` field is populated correctly
  - the email renders the right value
  - the workflow fires exactly once per contact

### Phase 3: Production rollout

- Push `monthly_rate` to the full target cohort.
- Apply the production trigger tag in a controlled batch.
- Monitor first sends before completing the remainder.

## Inputs Needed Before Building The Script

- The exact GHL custom field ID or internal key for `monthly_rate`
- The exact test workflow tag
- The exact production workflow tag
- Whether the script should support a small-batch mode such as `--limit 5`

## Current Business Rule

The current pricing logic in `scripts/skimmer_export_tagged_customer_rates.py` is:

- if base price is under `$200`, increase by `15%`
- if base price is `$200` or more, increase by `$20`

The script currently targets only Skimmer customers tagged `sri-042026`.
