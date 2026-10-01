from sqlalchemy import select
from .db import Company, User, Follow, Session

# Directory entries, not assertions that any particular program remains active.
COMPANIES = [
    ("ARVN", "Arvinas", "Arvinas", "https://www.arvinas.com"),
    ("BEAM", "Beam Therapeutics", "Beam Therapeutics", "https://beamtx.com"),
    ("BNTX", "BioNTech", "BioNTech", "https://www.biontech.com"),
    ("TNGX", "Tango Therapeutics", "Tango Therapeutics", "https://www.tangotx.com"),
    ("CGON", "CG Oncology", "CG Oncology", "https://cgoncology.com"),
    (
        "CRBU",
        "Caribou Biosciences",
        "Caribou Biosciences",
        "https://www.cariboubio.com",
    ),
    ("CRSP", "CRISPR Therapeutics", "CRISPR Therapeutics", "https://crisprtx.com"),
    (
        "DAWN",
        "Day One Biopharmaceuticals",
        "Day One Biopharmaceuticals",
        "https://www.dayonebio.com",
    ),
    ("IDYA", "IDEAYA Biosciences", "IDEAYA Biosciences", "https://www.ideayabio.com"),
    ("ERAS", "Erasca", "Erasca", "https://www.erasca.com"),
    ("EXEL", "Exelixis", "Exelixis", "https://www.exelixis.com"),
    (
        "FATE",
        "Fate Therapeutics",
        "Fate Therapeutics",
        "https://www.fatetherapeutics.com",
    ),
    ("GILD", "Gilead Sciences", "Gilead Sciences", "https://www.gilead.com"),
    ("GMAB", "Genmab", "Genmab", "https://www.genmab.com"),
    ("IMTX", "Immatics", "Immatics", "https://immatics.com"),
    ("INCY", "Incyte", "Incyte", "https://www.incyte.com"),
    (
        "IOVA",
        "Iovance Biotherapeutics",
        "Iovance Biotherapeutics",
        "https://www.iovance.com",
    ),
    ("KURA", "Kura Oncology", "Kura Oncology", "https://www.kuraoncology.com"),
    ("KYMR", "Kymera Therapeutics", "Kymera Therapeutics", "https://www.kymeratx.com"),
    ("AVBP", "ArriVent BioPharma", "ArriVent BioPharma", "https://arrivent.com"),
    ("NUVB", "Nuvation Bio", "Nuvation Bio", "https://www.nuvationbio.com"),
    ("RCUS", "Arcus Biosciences", "Arcus Biosciences", "https://arcusbio.com"),
    ("REPL", "Replimune", "Replimune", "https://www.replimune.com"),
    ("RVMD", "Revolution Medicines", "Revolution Medicines", "https://www.revmed.com"),
    (
        "SNDX",
        "Syndax Pharmaceuticals",
        "Syndax Pharmaceuticals",
        "https://www.syndax.com",
    ),
]


def seed():
    with Session() as db:
        for ticker, name, sponsor, website in COMPANIES:
            if not db.scalar(select(Company).where(Company.ticker == ticker)):
                db.add(
                    Company(ticker=ticker, name=name, sponsor=sponsor, website=website)
                )
        db.commit()
