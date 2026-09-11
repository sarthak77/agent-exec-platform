-- Sample business-domain tables (customers, invoices) for the integration
-- tests, e.g. an accounting-assistant-style task that looks up customers or
-- overdue invoices. Loaded once by ServiceManager.load_sample_data() after
-- the target database exists. Idempotent (IF NOT EXISTS / ON CONFLICT DO
-- NOTHING) so re-running the suite never errors or duplicates rows.
--
-- Deliberately varied (10 customers, 20 invoices spanning paid/overdue/
-- pending, amounts from ~3K to 250K, due dates across many months) so
-- queries filtering by customer, status, amount threshold, or due date all
-- have more than one matching/non-matching row to exercise.

CREATE TABLE IF NOT EXISTS customers (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    amount      NUMERIC NOT NULL,
    status      TEXT NOT NULL,
    due_date    DATE NOT NULL
);

INSERT INTO customers (id, tenant_id, name, email) VALUES
    ('CUST-001', 'integration-tenant', 'Acme Corp',         'billing@acme.example'),
    ('CUST-002', 'integration-tenant', 'Globex LLC',        'accounts@globex.example'),
    ('CUST-003', 'integration-tenant', 'Initech',           'ap@initech.example'),
    ('CUST-004', 'integration-tenant', 'Umbrella Corp',     'finance@umbrella.example'),
    ('CUST-005', 'integration-tenant', 'Wayne Enterprises', 'payments@wayne.example'),
    ('CUST-006', 'integration-tenant', 'Stark Industries',  'ap@stark.example'),
    ('CUST-007', 'integration-tenant', 'Wonka Industries',  'billing@wonka.example'),
    ('CUST-008', 'integration-tenant', 'Hooli',             'accounts@hooli.example'),
    ('CUST-009', 'integration-tenant', 'Soylent Corp',      'ap@soylent.example'),
    ('CUST-010', 'integration-tenant', 'Massive Dynamic',   'finance@massivedynamic.example')
ON CONFLICT (id) DO NOTHING;

INSERT INTO invoices (id, tenant_id, customer_id, amount, status, due_date) VALUES
    ('INV-1001', 'integration-tenant', 'CUST-001',  75000, 'overdue', '2026-07-15'),
    ('INV-1002', 'integration-tenant', 'CUST-002',  32000, 'paid',    '2026-06-01'),
    ('INV-1003', 'integration-tenant', 'CUST-003',  61000, 'overdue', '2026-08-01'),
    ('INV-1004', 'integration-tenant', 'CUST-001',  15000, 'pending', '2026-09-20'),
    ('INV-1005', 'integration-tenant', 'CUST-004', 120000, 'overdue', '2026-05-10'),
    ('INV-1006', 'integration-tenant', 'CUST-004',  45000, 'paid',    '2026-04-22'),
    ('INV-1007', 'integration-tenant', 'CUST-005',  98000, 'pending', '2026-10-05'),
    ('INV-1008', 'integration-tenant', 'CUST-005',   5000, 'paid',    '2026-03-01'),
    ('INV-1009', 'integration-tenant', 'CUST-006', 250000, 'overdue', '2026-06-30'),
    ('INV-1010', 'integration-tenant', 'CUST-006',  18000, 'pending', '2026-11-01'),
    ('INV-1011', 'integration-tenant', 'CUST-007',   3000, 'paid',    '2026-02-15'),
    ('INV-1012', 'integration-tenant', 'CUST-007',  72000, 'overdue', '2026-07-01'),
    ('INV-1013', 'integration-tenant', 'CUST-008',  55000, 'pending', '2026-09-25'),
    ('INV-1014', 'integration-tenant', 'CUST-008',   8000, 'paid',    '2026-01-20'),
    ('INV-1015', 'integration-tenant', 'CUST-009', 130000, 'overdue', '2026-08-20'),
    ('INV-1016', 'integration-tenant', 'CUST-009',  27000, 'paid',    '2026-05-05'),
    ('INV-1017', 'integration-tenant', 'CUST-010', 210000, 'pending', '2026-12-01'),
    ('INV-1018', 'integration-tenant', 'CUST-010',  64000, 'overdue', '2026-07-28'),
    ('INV-1019', 'integration-tenant', 'CUST-002',  12000, 'paid',    '2026-02-10'),
    ('INV-1020', 'integration-tenant', 'CUST-003',  89000, 'pending', '2026-10-15')
ON CONFLICT (id) DO NOTHING;
