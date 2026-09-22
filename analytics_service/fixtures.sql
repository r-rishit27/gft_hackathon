-- Tiny synthetic test fixture, NOT the deployed dataset or production results.
CREATE TABLE Party (
 party_id VARCHAR, validity_start_time TIMESTAMP, is_entity_deleted BOOLEAN,
 exit_date DATE, join_date DATE
);
INSERT INTO Party VALUES
 ('HASE_HK_P0001', '2026-01-01', FALSE, NULL, '2025-01-01'),
 ('HASE_HK_P0001', '2026-06-01', FALSE, NULL, '2025-01-01'),
 ('HASE_HK_P0002', '2026-01-01', FALSE, '2026-07-01', '2025-01-01'),
 ('HASE_HK_P0003', '2026-01-01', TRUE, NULL, '2025-01-01'),
 ('HASE_HK_P0004', '2026-09-02', FALSE, NULL, '2026-09-02');

CREATE TABLE Transaction (
 book_time TIMESTAMP, direction VARCHAR, is_entity_deleted BOOLEAN,
 normalized_booked_amount STRUCT(currency_code VARCHAR, units BIGINT, nanos BIGINT)
);
INSERT INTO Transaction VALUES
 ('2026-01-01', 'CREDIT', FALSE, {'currency_code':'USD','units':10,'nanos':500000000}),
 ('2026-01-02', 'CREDIT', FALSE, {'currency_code':'USD','units':20,'nanos':250000000}),
 ('2026-01-03', 'DEBIT', FALSE, {'currency_code':'USD','units':5,'nanos':0}),
 ('2026-02-01', 'CREDIT', FALSE, {'currency_code':'USD','units':45,'nanos':500000000}),
 ('2026-02-02', 'DEBIT', FALSE, {'currency_code':'USD','units':8,'nanos':750000000}),
 ('2026-03-01', 'CREDIT', FALSE, {'currency_code':'USD','units':38,'nanos':0}),
 ('2026-03-02', 'DEBIT', FALSE, {'currency_code':'USD','units':12,'nanos':0}),
 ('2026-04-01', 'CREDIT', FALSE, {'currency_code':'USD','units':65,'nanos':250000000}),
 ('2026-04-02', 'DEBIT', FALSE, {'currency_code':'USD','units':23,'nanos':500000000}),
 ('2026-05-01', 'CREDIT', FALSE, {'currency_code':'USD','units':72,'nanos':0}),
 ('2026-05-02', 'DEBIT', FALSE, {'currency_code':'USD','units':19,'nanos':250000000}),
 ('2026-06-01', 'CREDIT', FALSE, {'currency_code':'USD','units':62,'nanos':750000000}),
 ('2026-06-02', 'DEBIT', FALSE, {'currency_code':'USD','units':32,'nanos':0}),
 ('2026-07-01', 'CREDIT', FALSE, {'currency_code':'USD','units':90,'nanos':250000000}),
 ('2026-07-02', 'DEBIT', FALSE, {'currency_code':'USD','units':28,'nanos':500000000}),
 ('2026-08-01', 'CREDIT', FALSE, {'currency_code':'USD','units':105,'nanos':0}),
 ('2026-08-02', 'DEBIT', FALSE, {'currency_code':'USD','units':42,'nanos':750000000}),
 ('2026-08-03', 'CREDIT', TRUE, {'currency_code':'USD','units':999,'nanos':0});

CREATE TABLE RiskCaseEvent (risk_case_id VARCHAR, type VARCHAR, event_time TIMESTAMP);
INSERT INTO RiskCaseEvent VALUES
 ('C1','AML_ALERT_GOOGLE','2026-01-01'), ('C1','AML_ALERT_GOOGLE','2026-01-02'),
 ('C1','AML_PROCESS_START','2026-01-02'), ('C1','AML_SAR','2026-01-03'),
 ('C1','AML_SAR','2026-01-04'), ('C1','AML_PROCESS_END','2026-01-05'),
 ('C2','AML_ALERT_LEGACY','2026-01-01'), ('C2','AML_PROCESS_START','2026-01-02'),
 ('C3','AML_PROCESS_START','2026-01-01'), ('C3','AML_PROCESS_END','2026-01-02'),
 ('C3','AML_PROCESS_START','2026-01-03'), ('C4','AML_SAR','2026-02-01');

CREATE TABLE RiskScores (party_id VARCHAR, risk_period_end_time TIMESTAMP, risk_score DOUBLE);
INSERT INTO RiskScores VALUES
 ('HASE_HK_P0001','2026-01-31',0.2), ('HASE_HK_P0002','2026-01-31',0.8),
 ('HASE_HK_P0001','2026-02-28',0.6), ('HASE_HK_P0002','2026-02-28',NULL);
