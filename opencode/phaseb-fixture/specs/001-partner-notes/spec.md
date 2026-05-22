# Feature Specification: Partner Internal Notes

> Phase-B validation fixture — a small, coherent design spec used to exercise the
> Spec-Kit `/plan -> /tasks -> /analyze` pipeline on the live OpenCode service.

## User Scenarios & Testing

A salesperson opens a customer (`res.partner`) record and writes a free-text
internal note about that customer — for example a reminder about payment terms
or a delivery preference. The note is visible to other internal users who open
the same customer, and is never shown to the customer.

### Out of scope

- No rich-text formatting, attachments, or note history / threading.
- No visibility on customer-facing documents or the customer portal.

## Requirements

- The `res.partner` form view shows a multi-line "Internal Notes" text field.
- The note is stored on the partner record and persists across sessions.
- The field is internal-only: it must not appear on any customer-facing report,
  email, or portal page.
- Reading and writing the field follows the standard internal-user access rules
  for `res.partner` — no new security group is introduced.

## Success Criteria

- A user can open a partner, type a note, save, reopen the record, and see the
  saved note unchanged.
- The note text does not appear on the sales-order PDF or the customer portal
  partner page.
- The feature reuses `res.partner` access rules and adds no new security group.
