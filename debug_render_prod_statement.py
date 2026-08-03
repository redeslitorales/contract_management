from datetime import date

partner = env["res.partner"].browse(10174).exists()
if not partner:
    partner = env["res.partner"].search([("customer_rank", ">", 0)], limit=1)
wizard = env["customer.account.statement.wizard"].create({
    "partner_id": partner.commercial_partner_id.id,
    "company_id": env.company.id,
    "date_from": date(2024, 1, 1),
    "date_to": date(2026, 8, 2),
})
report = env.ref("contract_management.action_report_customer_account_statement")
pdf, report_type = report.with_context(lang="es_419")._render_qweb_pdf(
    report.report_name,
    res_ids=wizard.ids,
)
with open("/tmp/account_statement_prod_check.pdf", "wb") as output:
    output.write(pdf)
print("STATEMENT_RENDER", wizard.id, len(pdf), report_type)
