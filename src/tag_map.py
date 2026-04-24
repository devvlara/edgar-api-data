from __future__ import annotations

# metric -> ordered list of XBRL us-gaap tags to try. First tag that has data wins.
TAG_MAP: dict[str, list[str]] = {

    # ── Income Statement ──────────────────────────────────────────────────────
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ],
    "cost_of_revenue": [
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
    ],
    "gross_profit": [
        "GrossProfit",
    ],
    "r_and_d": [
        "ResearchAndDevelopmentExpense",
        "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
    ],
    "sga_expense": [
        "SellingGeneralAndAdministrativeExpense",
    ],
    "operating_income": [
        "OperatingIncomeLoss",
    ],
    "interest_expense": [
        "InterestExpense",
        "InterestAndDebtExpense",
        "InterestExpenseDebt",
    ],
    "interest_income": [
        "InvestmentIncomeInterest",
        "InterestIncomeOperating",
        "InterestAndOtherIncome",
    ],
    "pretax_income": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ],
    "income_tax": [
        "IncomeTaxExpenseBenefit",
    ],
    "net_income": [
        "NetIncomeLoss",
        "NetIncome",
        "ProfitLoss",
    ],
    "depreciation_amortization": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
        "Depreciation",
    ],
    "stock_based_compensation": [
        "ShareBasedCompensation",
        "AllocatedShareBasedCompensationExpense",
    ],
    "eps_basic": [
        "EarningsPerShareBasic",
    ],
    "eps_diluted": [
        "EarningsPerShareDiluted",
    ],
    "shares_basic": [
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ],
    "shares_diluted": [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
    ],

    # ── Balance Sheet — Assets ────────────────────────────────────────────────
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashAndCashEquivalents",
        "Cash",
    ],
    "short_term_investments": [
        "ShortTermInvestments",
        "MarketableSecuritiesCurrent",
        "AvailableForSaleSecuritiesCurrent",
    ],
    "accounts_receivable": [
        "AccountsReceivableNetCurrent",
        "ReceivablesNetCurrent",
    ],
    "inventory": [
        "InventoryNet",
        "InventoryGross",
    ],
    "total_current_assets": [
        "AssetsCurrent",
    ],
    "ppe_net": [
        "PropertyPlantAndEquipmentNet",
        "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
    ],
    "goodwill": [
        "Goodwill",
    ],
    "intangible_assets": [
        "IntangibleAssetsNetExcludingGoodwill",
        "FiniteLivedIntangibleAssetsNet",
    ],
    "long_term_investments": [
        "LongTermInvestments",
        "MarketableSecuritiesNoncurrent",
        "AvailableForSaleSecuritiesNoncurrent",
    ],
    "total_assets": [
        "Assets",
    ],

    # ── Balance Sheet — Liabilities & Equity ─────────────────────────────────
    "accounts_payable": [
        "AccountsPayableCurrent",
    ],
    "short_term_debt": [
        "ShortTermBorrowings",
        "LongTermDebtCurrent",
        "DebtCurrent",
        "CommercialPaper",
    ],
    "deferred_revenue_current": [
        "DeferredRevenueCurrent",
        "ContractWithCustomerLiabilityCurrent",
    ],
    "total_current_liabilities": [
        "LiabilitiesCurrent",
    ],
    "long_term_debt": [
        "LongTermDebtNoncurrent",
        "LongTermDebt",
        "LongTermNotesPayable",
    ],
    "deferred_revenue_noncurrent": [
        "DeferredRevenueNoncurrent",
        "ContractWithCustomerLiabilityNoncurrent",
    ],
    "total_liabilities": [
        "Liabilities",
    ],
    "retained_earnings": [
        "RetainedEarningsAccumulatedDeficit",
        "RetainedEarnings",
    ],
    "treasury_stock": [
        "TreasuryStockValue",
        "TreasuryStockCommonValue",
    ],
    "stockholders_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],

    # ── Cash Flow Statement ───────────────────────────────────────────────────
    "operating_cf": [
        "NetCashProvidedByUsedInOperatingActivities",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForCapitalImprovements",
    ],
    "purchases_of_investments": [
        "PaymentsToAcquireInvestments",
        "PaymentsToAcquireMarketableSecurities",
        "PaymentsToAcquireAvailableForSaleSecurities",
    ],
    "proceeds_from_investments": [
        "ProceedsFromSaleAndMaturityOfMarketableSecurities",
        "ProceedsFromMaturitiesPrepaymentsAndCallsOfAvailableForSaleSecurities",
        "ProceedsFromSaleOfInvestments",
    ],
    "acquisitions": [
        "PaymentsToAcquireBusinessesNetOfCashAcquired",
        "PaymentsToAcquireBusinessesGross",
    ],
    "investing_cf": [
        "NetCashProvidedByUsedInInvestingActivities",
    ],
    "debt_issuance": [
        "ProceedsFromIssuanceOfLongTermDebt",
        "ProceedsFromIssuanceOfDebt",
        "ProceedsFromDebtMaturingInMoreThanThreeMonths",
    ],
    "debt_repayment": [
        "RepaymentsOfLongTermDebt",
        "RepaymentsOfDebt",
        "RepaymentsOfLongTermDebtAndCapitalSecurities",
    ],
    "stock_buybacks": [
        "PaymentsForRepurchaseOfCommonStock",
        "PaymentsForRepurchaseOfEquity",
    ],
    "dividends_paid": [
        "PaymentsOfDividends",
        "PaymentsOfDividendsCommonStock",
        "PaymentsOfOrdinaryDividends",
    ],
    "financing_cf": [
        "NetCashProvidedByUsedInFinancingActivities",
    ],
}

# Metrics whose XBRL unit is not USD. Everything else defaults to USD.
UNIT_OVERRIDE: dict[str, str] = {
    "eps_basic":      "USD/shares",
    "eps_diluted":    "USD/shares",
    "shares_basic":   "shares",
    "shares_diluted": "shares",
}
