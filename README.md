# EDGAR API Data Project

A data project built on the U.S. Securities and Exchange Commission's free, public [EDGAR API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces). This repository is the exploration phase: documentation of every available endpoint, a field-level data dictionary, and an entity-relationship diagram of the underlying data model. Code and analyses will grow from here.

## What's in this repo

```
edgar-api-data-project/
├── README.md                 ← you are here
├── .gitignore
├── requirements.txt          ← Python dependencies
├── GITHUB_SETUP.md           ← first-time setup walkthrough
├── docs/                     ← reference documentation
│   ├── edgar-api-exploration.md         narrative guide to the 10 endpoints
│   ├── edgar-api-data-dictionary.md     field-level dictionary (markdown)
│   ├── edgar-api-data-dictionary.xlsx   same dictionary as a 12-tab workbook
│   ├── edgar-api-erd.md                 entity-relationship diagram + schema
│   └── edgar-api-erd.mermaid            standalone Mermaid diagram source
├── src/                      ← Python source code (packages/modules)
├── notebooks/                ← Jupyter notebooks for exploration
├── data/                     ← local data (gitignored — see .gitignore)
│   ├── raw/                  ← raw API responses
│   └── processed/            ← cleaned / transformed data
└── tests/                    ← unit tests
```

## Quick start

```bash
# 1. clone the repo
git clone https://github.com/<your-username>/edgar-api-data-project.git
cd edgar-api-data-project

# 2. create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. install dependencies
pip install -r requirements.txt

# 4. read the docs, then start exploring
jupyter notebook notebooks/
```

## Using the EDGAR API

The SEC requires every request to include a descriptive `User-Agent` header and limits traffic to **10 requests per second** per IP. Both rules are enforced at the edge — break either and you'll get a temporary block.

Recommended header format:
```
User-Agent: Your Name your.email@example.com
```

See [`docs/edgar-api-exploration.md`](docs/edgar-api-exploration.md) for a complete tour of the endpoints and [`docs/edgar-api-data-dictionary.md`](docs/edgar-api-data-dictionary.md) for field-level details.

## Status

- [x] Endpoint exploration & reference guide
- [x] Field-level data dictionary (markdown + xlsx)
- [x] Entity-relationship diagram + suggested schema
- [ ] Python client module (`src/edgar_client.py`)
- [ ] First analysis notebook
- [ ] Database loader

## References

- [EDGAR API documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- [Accessing EDGAR data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)
- [SEC Developer Resources](https://www.sec.gov/about/developer-resources)
