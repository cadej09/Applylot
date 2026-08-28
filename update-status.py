"""Stamp response_status on applied jobs from the 2026-08-04 inbox audit."""
import sqlite3

c = sqlite3.connect(r"C:\Users\you\.applypilot\applypilot.db", timeout=30)

UPDATES = [
    # (label, sql WHERE fragment)
    ("rejected: Amazon BIE I R2L Analytics",
     "(url LIKE '%10487597%' OR COALESCE(application_url,'') LIKE '%10487597%')"),
    ("rejected: Amazon BIE WW FBA Central Analytics",
     "(url LIKE '%10487507%' OR COALESCE(application_url,'') LIKE '%10487507%')"),
    ("rejected: Amazon BIE WHS Data",
     "(url LIKE '%10484142%' OR COALESCE(application_url,'') LIKE '%10484142%')"),
    ("rejected: Amazon DS II SCOT OSS",
     "(url LIKE '%10485407%' OR COALESCE(application_url,'') LIKE '%10485407%')"),
    ("rejected: Amazon BIE Supply Chain Bulk Fulfillment",
     "(url LIKE '%10485425%' OR COALESCE(application_url,'') LIKE '%10485425%')"),
    ("rejected: Zoom Machine Learning Engineer",
     "COALESCE(company,'') LIKE '%Zoom%' AND title = 'Machine Learning Engineer'"),
    ("rejected: Nintendo Retail Marketing Coordinator",
     "COALESCE(company,'') LIKE '%Nintendo%' AND title LIKE '%Retail Marketing%'"),
    ("rejected: City of Kenmore IT Systems Analyst",
     "COALESCE(company,'') LIKE '%Kenmore%' AND title LIKE '%IT Systems Analyst%'"),
    ("rejected: Quora ML Engineer New Grad (Ashby email 08-03)",
     "COALESCE(company,'') LIKE '%Quora%' AND title LIKE '%Machine Learning Engineer, New Grad%'"),
    ("assessment_invited: BNSF Data Scientist I/II (Codility)",
     "COALESCE(company,'') LIKE '%BNSF%'"),
    ("interview_pending: People Tech Group Data Engineer (Jr. DS interview thread)",
     "COALESCE(company,'') LIKE '%People Tech%'"),
    ("confirmed: Microsoft Data Engineer II",
     "COALESCE(company,'') LIKE '%Microsoft%' AND title LIKE '%Data Engineer II%'"),
    ("confirmed: Amazon BIE Amazon Freight",
     "(url LIKE '%10488433%' OR COALESCE(application_url,'') LIKE '%10488433%')"),
    # --- deep sweep 08-04 (mail 07-05 -> 07-28) ---
    ("rejected: Amazon Transportation Analyst STREAM BI (10445141)",
     "title LIKE '%Transportation Analyst, STREAM BI%'"),
    ("rejected: Amazon Applied Scientist II Gen AI LLM PXT (3162762)",
     "title LIKE '%Applied Scientist II - Gen AI%'"),
    ("rejected: Amazon SDE Amazon Payments (10414304)",
     "title LIKE '%Software Dev Engineer, Amazon Payments%'"),
    ("rejected: Amazon Finance Analyst Flywheel (10446574)",
     "title LIKE '%Finance Analyst Flywheel%'"),
    ("rejected: Amazon DS II SCOT OSS cross-board dupe",
     "title LIKE '%Data Scientist II, SCOT OSS%'"),
    ("rejected: Amazon BIE WHS Data cross-board dupe",
     "title LIKE '%Business Intelligence Engineer, WHS Data%'"),
    ("rejected: Amazon BIE Workforce Planning One Medical (10419986)",
     "title LIKE '%Business Intelligence Engineer, Workforce Pla%'"),
    ("rejected: Amazon DE Data Science Focus ASAT (10461781)",
     "title LIKE '%Data Engineer- Data Science Focus%'"),
    ("rejected: Blue Origin Financial Business Partner III",
     "title LIKE '%Financial Business Partner III%'"),
    ("rejected: Cast AI Growth Performance Engineer",
     "title LIKE '%Growth Performance Engineer%'"),
    ("rejected: Purpose Brands Data Engineers Seattle",
     "title LIKE '%Data Engineers Seattle%'"),
    ("rejected: Cisco Applied AI Scientist (Req 2018334)",
     "(url LIKE '%2018334%' OR COALESCE(application_url,'') LIKE '%2018334%')"),
    ("rejected: BECU Data Scientist",
     "(site LIKE '%becu%' OR url LIKE '%becu%') AND title = 'Data Scientist'"),
    ("confirmed: Amazon DS II Currency Convertor",
     "title LIKE '%Amazon Currency Convert%' AND response_status IS NULL"),
    ("confirmed: Amazon Intelligence Analyst GSI",
     "title LIKE '%Intelligence Analyst, Global Security%' AND response_status IS NULL"),
    ("confirmed: Amazon DE Prime Video Core Analytics",
     "title LIKE '%Prime Video Core Analytics%' AND response_status IS NULL"),
]

STATUS = {
    "rejected: Amazon BIE I R2L Analytics": "rejected",
    "rejected: Amazon BIE WW FBA Central Analytics": "rejected",
    "rejected: Amazon BIE WHS Data": "rejected",
    "rejected: Amazon DS II SCOT OSS": "rejected",
    "rejected: Amazon BIE Supply Chain Bulk Fulfillment": "rejected",
    "rejected: Zoom Machine Learning Engineer": "rejected",
    "rejected: Nintendo Retail Marketing Coordinator": "rejected",
    "rejected: City of Kenmore IT Systems Analyst": "rejected",
    "rejected: Quora ML Engineer New Grad (Ashby email 08-03)": "rejected",
    "assessment_invited: BNSF Data Scientist I/II (Codility)": "assessment_invited",
    "interview_pending: People Tech Group Data Engineer (Jr. DS interview thread)": "interview_pending",
    "confirmed: Microsoft Data Engineer II": "confirmed",
    "confirmed: Amazon BIE Amazon Freight": "confirmed",
    "rejected: Amazon Transportation Analyst STREAM BI (10445141)": "rejected",
    "rejected: Amazon Applied Scientist II Gen AI LLM PXT (3162762)": "rejected",
    "rejected: Amazon SDE Amazon Payments (10414304)": "rejected",
    "rejected: Amazon Finance Analyst Flywheel (10446574)": "rejected",
    "rejected: Amazon DS II SCOT OSS cross-board dupe": "rejected",
    "rejected: Amazon BIE WHS Data cross-board dupe": "rejected",
    "rejected: Amazon BIE Workforce Planning One Medical (10419986)": "rejected",
    "rejected: Amazon DE Data Science Focus ASAT (10461781)": "rejected",
    "rejected: Blue Origin Financial Business Partner III": "rejected",
    "rejected: Cast AI Growth Performance Engineer": "rejected",
    "rejected: Purpose Brands Data Engineers Seattle": "rejected",
    "rejected: Cisco Applied AI Scientist (Req 2018334)": "rejected",
    "rejected: BECU Data Scientist": "rejected",
    "confirmed: Amazon DS II Currency Convertor": "confirmed",
    "confirmed: Amazon Intelligence Analyst GSI": "confirmed",
    "confirmed: Amazon DE Prime Video Core Analytics": "confirmed",
}

for label, where in UPDATES:
    n = c.execute(
        f"UPDATE jobs SET response_status=? WHERE apply_status='applied' AND {where}",
        (STATUS[label],)).rowcount
    print(f"  {label}: {n} row(s)")

# Mark plain email confirmations (received in inbox, no decision yet)
n = c.execute(
    "UPDATE jobs SET response_status='confirmed' "
    "WHERE apply_status='applied' AND response_status IS NULL "
    "AND applied_at > datetime('now','-4 days')").rowcount
print(f"  confirmed (recent, no decision yet): {n} row(s)")

c.commit()
rows = c.execute(
    "SELECT COALESCE(response_status,'(none)') s, COUNT(*) FROM jobs "
    "WHERE apply_status='applied' GROUP BY s ORDER BY 2 DESC").fetchall()
print("\nResponse-status summary of all applied jobs:")
for s, n in rows:
    print(f"  {s:20s} {n}")
c.close()
