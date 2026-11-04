import xml.etree.ElementTree as ET
from datetime import date, timedelta
from typing import Any

_XBRL_CRORE_TAGS = {
    # P&L
    "RevenueFromOperations", "OtherIncome", "Income",
    "EmployeeBenefitExpense", "FinanceCosts",
    "DepreciationDepletionAndAmortisationExpense", "OtherExpenses", "Expenses",
    "ProfitBeforeExceptionalItemsAndTax", "ExceptionalItems",
    "ProfitBeforeTax", "TaxExpense", "TaxExpenseOfDiscontinuedOperations",
    "ProfitLossForPeriod", "ProfitLossForPeriodFromContinuingOperations",
    "ProfitLossFromDiscontinuedOperationsAfterTax", "ProfitLossFromDiscontinuedOperationsBeforeTax",
    "ProfitOrLossAttributableToOwnersOfParent", "ProfitOrLossAttributableToNonControllingInterests",
    "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade",
    "CostOfMaterialsConsumed", "PurchasesOfStockInTrade",
    # OCI
    "OtherComprehensiveIncome", "OtherComprehensiveIncomeNetOfTaxes",
    "AmountOfItemThatWillBeReclassifiedToProfitAndLoss",
    "AmountOfItemThatWillNotBeReclassifiedToProfitAndLoss",
    "IncomeTaxRelatingToItemsThatWillBeReclassifiedToProfitOrLoss",
    "IncomeTaxRelatingToItemsThatWillNotBeReclassifiedToProfitOrLoss",
    "ComprehensiveIncomeForThePeriod",
    "ComprehensiveIncomeForThePeriodAttributableToOwnersOfParent",
    "ComprehensiveIncomeForThePeriodAttributableToOwnersOfParentNonControllingInterests",
    # Balance Sheet — Assets
    "Assets", "CurrentAssets", "NoncurrentAssets", "EquityAndLiabilities",
    "CashAndCashEquivalents", "BankBalanceOtherThanCashAndCashEquivalents",
    "CurrentInvestments", "NoncurrentInvestments",
    "CurrentFinancialAssets", "NoncurrentFinancialAssets",
    "TradeReceivablesCurrent", "TradeReceivablesNoncurrent",
    "LoansCurrent", "LoansNoncurrent",
    "OtherCurrentAssets", "OtherNoncurrentAssets",
    "OtherCurrentFinancialAssets", "OtherNoncurrentFinancialAssets",
    "PropertyPlantAndEquipment", "CapitalWorkInProgress",
    "Goodwill", "OtherIntangibleAssets", "IntangibleAssetsUnderDevelopment",
    "DeferredTaxAssetsNet", "CurrentTaxAssets",
    "Inventories", "InvestmentProperty", "InvestmentsAccountedForUsingEquityMethod",
    "BiologicalAssetsOtherThanBearerPlants",
    # Balance Sheet — Equity & Liabilities
    "Equity", "EquityShareCapital", "OtherEquity", "NonControllingInterest",
    "PaidUpValueOfEquityShareCapital", "EquityAttributableToOwnersOfParent",
    "Liabilities", "CurrentLiabilities", "NoncurrentLiabilities",
    "CurrentFinancialLiabilities", "NoncurrentFinancialLiabilities",
    "BorrowingsCurrent", "BorrowingsNoncurrent",
    "TradePayablesCurrent", "TradePayablesNoncurrent",
    "TotalOutstandingDuesOfMicroEnterpriseAndSmallEnterpriseCurrent",
    "TotalOutstandingDuesOfCreditorsOtherThanMicroEnterpriseAndSmallEnterpriseCurrent",
    "TotalOutstandingDuesOfMicroEnterpriseAndSmallEnterpriseNoncurrent",
    "TotalOutstandingDuesOfCreditorsOtherThanMicroEnterpriseAndSmallEnterpriseNoncurrent",
    "OtherCurrentLiabilities", "OtherNoncurrentLiabilities",
    "OtherCurrentFinancialLiabilities", "OtherNoncurrentFinancialLiabilities",
    "ProvisionsCurrent", "ProvisionsNoncurrent",
    "DeferredTaxLiabilitiesNet", "DeferredTax", "CurrentTaxLiabilities", "CurrentTax",
    "DeferredGovernmentGrantsCurrent", "DeferredGovernmentGrantsNoncurrent",
    # Cash Flow — Totals
    "CashFlowsFromUsedInOperatingActivities", "CashFlowsFromUsedInInvestingActivities",
    "CashFlowsFromUsedInFinancingActivities", "CashFlowsFromUsedInOperations",
    "CashAndCashEquivalentsCashFlowStatement",
    "IncreaseDecreaseInCashAndCashEquivalents",
    "IncreaseDecreaseInCashAndCashEquivalentsBeforeEffectOfExchangeRateChanges",
    "EffectOfExchangeRateChangesOnCashAndCashEquivalents",
    # Cash Flow — Key line items
    "DividendsPaidClassifiedAsFinancingActivities",
    "IncomeTaxesPaidRefundClassifiedAsOperatingActivities",
    "IncomeTaxesPaidRefundClassifiedAsInvestingActivities",
    "IncomeTaxesPaidRefundClassifiedAsFinancingActivities",
    "InterestReceivedClassifiedAsInvestingActivities",
    "InterestReceivedClassifiedAsOperatingActivities",
    "InterestPaidClassifiedAsOperatingActivities",
    "InterestPaidClassifiedAsFinancingActivities",
    "DividendsReceivedClassifiedAsInvestingActivities",
    "DividendsReceivedClassifiedAsOperatingActivities",
    "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
    "PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities",
    "PurchaseOfIntangibleAssetsUnderDevelopment",
    "PurchaseOfInvestmentPropertyClassifiedAsInvestingActivities",
    "PurchaseOfOtherLongTermAssetsClassifiedAsInvestingActivities",
    "CashFlowsUsedInObtainingControlOfSubsidiariesOrOtherBusinessesClassifiedAsInvestingActivities",
    "CashFlowsFromLosingControlOfSubsidiariesOrOtherBusinessesClassifiedAsInvestingActivities",
    "ProceedsFromSalesOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
    "ProceedsFromSalesOfIntangibleAssetsClassifiedAsInvestingActivities",
    "ProceedsFromBorrowingsClassifiedAsFinancingActivities",
    "RepaymentsOfBorrowingsClassifiedAsFinancingActivities",
    "ProceedsFromIssuingSharesClassifiedAsFinancingActivities",
    "PaymentsToAcquireOrRedeemEntitysShares",
    "ProceedsFromExerciseOfStockOptions",
    "PaymentsOfLeaseLiabilitiesClassifiedAsFinancingActivities",
    "OtherInflowsOutflowsOfCashClassifiedAsOperatingActivities",
    "OtherInflowsOutflowsOfCashClassifiedAsInvestingActivities",
    "OtherInflowsOutflowsOfCashClassifiedAsFinancingActivities",
    # Cash Flow — Working capital adjustments
    "AdjustmentsForReconcileProfitLoss",
    "AdjustmentsForDepreciationAndAmortisationExpense",
    "AdjustmentsForFinanceCosts", "AdjustmentsForInterestIncome",
    "AdjustmentsForDividendIncome", "AdjustmentsForFairValueGainsLosses",
    "AdjustmentsForSharebasedPayments", "AdjustmentsForUnrealisedForeignExchangeLossesGains",
    "AdjustmentsForImpairmentLossReversalOfImpairmentLossRecognisedInProfitOrLoss",
    "AdjustmentsForDecreaseIncreaseInInventories",
    "AdjustmentsForDecreaseIncreaseInTradeReceivablesCurrent",
    "AdjustmentsForDecreaseIncreaseInTradeReceivablesNoncurrent",
    "AdjustmentsForDecreaseIncreaseInOtherCurrentAssets",
    "AdjustmentsForDecreaseIncreaseInOtherNoncurrentAssets",
    "AdjustmentsForIncreaseDecreaseInTradePayablesCurrent",
    "AdjustmentsForIncreaseDecreaseInTradePayablesNoncurrent",
    "AdjustmentsForIncreaseDecreaseInOtherCurrentLiabilities",
    "AdjustmentsForIncreaseDecreaseInOtherNoncurrentLiabilities",
    "AdjustmentsForProvisionsCurrent", "AdjustmentsForProvisionsNoncurrent",
    "AdjustmentsForOtherBankBalances",
    "AdjustmentsForOtherFinancialAssetsCurrent", "AdjustmentsForOtherFinancialAssetsNoncurrent",
    "AdjustmentsForOtherFinancialLiabilitiesCurrent", "AdjustmentsForOtherFinancialLiabilitiesNoncurrent",
    "AdjustmentsForUndistributedProfitsOfAssociates",
    "OtherAdjustmentsForNoncashItems", "OtherAdjustmentsToReconcileProfitLoss",
    "OtherAdjustmentsForWhichCashEffectsAreInvestingOrFinancingCashFlow",
    # Segments
    "SegmentRevenue", "SegmentRevenueFromOperations",
    "SegmentProfitBeforeTax", "SegmentProfitLossBeforeTaxAndFinanceCosts",
    "SegmentFinanceCosts", "SegmentAssets", "SegmentLiabilities",
    "NetSegmentAssets", "NetSegmentLiabilities",
    "UnAllocableAssets", "UnAllocableLiabilities",
    "OtherUnallocableExpenditureNetOffUnAllocableIncome", "InterSegmentRevenue",
}
_XBRL_META_TAGS = {
    "Symbol", "NameOfTheCompany", "ReportingQuarter",
    "DateOfStartOfReportingPeriod", "DateOfEndOfReportingPeriod",
    "DateOfStartOfFinancialYear", "DateOfEndOfFinancialYear",
    "DateOfBoardMeetingWhenFinancialResultsWereApproved",
    "NatureOfReportStandaloneConsolidated", "LevelOfRounding",
    "WhetherResultsAreAuditedOrUnaudited",
    "TypeOfCompany", "TypeOfReportingPeriod", "TypeOfCashFlowStatement",
    "IsCompanyReportingMultisegmentOrSingleSegment",
    "AuditorsFirmName", "ClassOfSecurity", "ISIN", "MSEISymbol",
    "WhetherCashFlowStatementIsApplicableOnCompany",
    "DescriptionOfReportableSegment",
}
_XBRL_EPS_TAGS = {
    "BasicEarningsLossPerShareFromContinuingOperations",
    "DilutedEarningsLossPerShareFromContinuingOperations",
    "BasicEarningsLossPerShareFromTotalOperations",
    "DilutedEarningsLossPerShareFromTotalOperations",
    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    "BasicEarningsLossPerShareFromDiscontinuedOperations",
    "DilutedEarningsLossPerShareFromDiscontinuedOperations",
}
_XBRL_SMALL_NUM_TAGS = {"FaceValueOfEquityShareCapital", "ScripCode"}
_ALL_XBRL_TAGS = _XBRL_CRORE_TAGS | _XBRL_META_TAGS | _XBRL_EPS_TAGS | _XBRL_SMALL_NUM_TAGS


def _parse_xbrl(xml_bytes: bytes) -> dict:
    """Parse an NSE Ind-AS XBRL file into a flat dict; monetary values in ₹ Crores."""
    root = ET.fromstring(xml_bytes)
    raw: dict = {}
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag in _ALL_XBRL_TAGS and tag not in raw:
            raw[tag] = (elem.text or "").strip()

    crore = 1e7
    out: dict = {}
    for k, v in raw.items():
        if not v:
            continue
        try:
            num = float(v)
            if k in _XBRL_EPS_TAGS or k in _XBRL_SMALL_NUM_TAGS:
                out[k] = round(num, 2)
            elif k in _XBRL_CRORE_TAGS:
                out[k] = round(num / crore, 2)
            else:
                out[k] = v
        except ValueError:
            out[k] = v
    return out


def _fmt(d: date) -> str:
    return d.strftime("%d-%m-%Y")


class CorporateMixin:
    """Corporate events: actions, announcements, results, filings."""

    def corporate_actions(
        self,
        symbol:    str | None = None,
        from_date: date | None = None,
        to_date:   date | None = None,
        series:    str = "EQ",
        segment:   str = "equities",
    ) -> list[dict]:
        """
        Dividends, splits, bonuses, rights issues, buy-backs.
        segment: 'equities' | 'sme' | 'debt'
        Omit symbol to get all upcoming actions.
        """
        to_date   = to_date   or date.today()
        from_date = from_date or (to_date - timedelta(days=30))
        params: dict = {
            "index":  segment,
            "series": series.upper(),
            "from":   _fmt(from_date),
            "to":     _fmt(to_date),
        }
        if symbol:
            params["symbol"] = symbol.upper()
        resp = self._session.get("corporates-corporateActions", params=params)
        return resp if isinstance(resp, list) else resp.get("data", resp)

    def announcements(
        self,
        symbol:    str | None = None,
        index:     str = "equities",
        from_date: date | None = None,
        to_date:   date | None = None,
        category:  str | None = None,
    ) -> list[dict]:
        """
        Exchange announcements / disclosures.
        index: 'equities' | 'sme' | 'debt' | 'mf' | 'invit'
        category: filter by announcement type via the NSE 'subject' param, e.g.
            'Financial Result Updates', 'Outcome of Board Meeting', 'Dividend',
            'Acquisition', 'Updates', 'Press Release', 'Analysts/Institutional
            Investor Meet/Con. Call Updates'. Value must match the 'desc' field
            exactly (NSE is case-sensitive and has minor spelling variants).

        NSE's API ignores the from/to params for large symbols and returns the
        full history. This method filters client-side to enforce the date range.
        """
        to_date   = to_date   or date.today()
        from_date = from_date or (to_date - timedelta(days=7))
        params: dict = {
            "index": index,
            "from":  _fmt(from_date),
            "to":    _fmt(to_date),
        }
        if symbol:
            params["symbol"] = symbol.upper()
        if category:
            params["subject"] = category
        resp = self._session.get("corporate-announcements", params=params)
        rows = resp if isinstance(resp, list) else resp.get("data", resp)

        # NSE sometimes ignores date params — enforce the range client-side
        from_iso = from_date.isoformat()
        to_iso   = to_date.isoformat()
        filtered = [
            r for r in rows
            if from_iso <= r.get("sort_date", "")[:10] <= to_iso
        ]

        # If category was passed, also enforce it client-side (NSE spelling variants)
        if category:
            cat_lower = category.lower()
            filtered = [r for r in filtered if r.get("desc", "").lower() == cat_lower]

        return filtered

    def all_announcements(
        self,
        from_date: date | None = None,
        to_date:   date | None = None,
        index:     str = "equities",
        category:  str | None = None,
        comp_name: str | None = None,
    ) -> list[dict]:
        """
        Bulk download of ALL announcements for a date range — no 20-record cap.

        Uses the CSV download endpoint (from_date/to_date params) that the NSE
        website's Download (.csv) button calls. Returns parsed rows as dicts.

        index:     'equities' | 'sme' | 'debt' | 'mf' | 'invit' | 'sse' |
                   'municipalBond' | 'invitsreits' | 'dtDisclosures' | 'craDisclosures'
        comp_name: company name filter (required by NSE when date range >= 30 days)
        """
        import csv, io
        to_date   = to_date   or date.today()
        from_date = from_date or to_date

        params = {
            "index":     index,
            "from_date": _fmt(from_date),
            "to_date":   _fmt(to_date),
            "csv":       "true",
        }
        if category:
            params["subject"] = category
        if comp_name:
            params["comp_name"] = comp_name

        resp = self._session._s.get(
            "https://www.nseindia.com/api/corporate-announcements",
            params=params,
            timeout=30,
        )
        resp.raise_for_status()

        # Strip UTF-8 BOM if present, then parse
        text = resp.content.decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for row in reader:
            # Strip surrounding quotes NSE adds to header names
            clean = {k.strip('"'): v for k, v in row.items()}
            rows.append(clean)

        if category:
            cat_lower = category.lower()
            rows = [r for r in rows if r.get("SUBJECT", "").lower() == cat_lower]

        return rows

    def board_meetings(
        self,
        symbol:    str | None = None,
        from_date: date | None = None,
        to_date:   date | None = None,
    ) -> list[dict]:
        """Scheduled and past board meetings with agenda."""
        to_date   = to_date   or date.today()
        from_date = from_date or (to_date - timedelta(days=30))
        params: dict = {
            "index": "equities",
            "from":  _fmt(from_date),
            "to":    _fmt(to_date),
        }
        if symbol:
            params["symbol"] = symbol.upper()
        resp = self._session.get("corporate-board-meetings", params=params)
        return resp if isinstance(resp, list) else resp.get("data", resp)

    def board_meeting_detail(self, xml_url: str) -> dict:
        """
        Parsed fields from a board meeting XBRL XML (the `attachment` URL from board_meetings()).
        Returns dict with keys: symbol, company, isin, meeting_type, intimation_type,
        meeting_date, report_date, trading_window_closed, trading_window_start,
        trading_window_end, agenda, results_audited, results_nature, reporting_period,
        reporting_month, reporting_year, dividend_type, and any extra tags.
        """
        r = self._session._s.get(xml_url, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)

        # strip namespace from tag name
        def local(tag: str) -> str:
            return tag.split("}")[-1] if "}" in tag else tag

        raw: dict[str, str] = {}
        for el in root.iter():
            name = local(el.tag)
            text = (el.text or "").strip()
            if text and name not in ("xbrl", "context", "unit", "schemaRef",
                                     "entity", "identifier", "period",
                                     "instant", "startDate", "endDate", "measure"):
                raw[name] = text

        def get(*keys: str) -> str | None:
            for k in keys:
                if k in raw:
                    return raw.pop(k)
            return None

        result: dict = {
            "symbol":                get("NSESymbol"),
            "company":               get("NameOfTheCompany"),
            "isin":                  get("ISIN"),
            "meeting_type":          get("TypeOfMeeting"),
            "intimation_type":       get("TypeOfIntimation"),
            "meeting_date":          get("DateOfProposedMeeting"),
            "report_date":           get("DateOfReport"),
            "trading_window_closed": get("WhetherTradingWindowIsClosed"),
            "trading_window_start":  get("DateOfStartOfTradingWindowClosure"),
            "trading_window_end":    get("DetailsOfDateOfEndOfTradingWindowClosure"),
            "results_audited":       get("WhetherResultsAreAuditedOrUnaudited"),
            "results_nature":        get("NatureOfReportStandaloneConsolidated"),
            "reporting_period":      get("ReportingPeriodType"),
            "reporting_month":       get("MonthOfPeriodEnded"),
            "reporting_year":        get("YearOfPeriodEnded"),
            "dividend_type":         get("TypeOfDividend"),
        }

        # collect agenda items (Agenda1, Agenda2, …)
        agenda = []
        for i in range(1, 20):
            val = get(f"Agenda{i}")
            if val:
                agenda.append(val)
            else:
                break
        result["agenda"] = agenda

        # any remaining unrecognised tags
        result["extra"] = raw

        return {k: v for k, v in result.items() if v not in (None, [], {})}

    def financial_results(
        self,
        symbol:    str | None = None,
        period:    str = "Quarterly",
        from_date: date | None = None,
        to_date:   date | None = None,
    ) -> list[dict]:
        """
        Quarterly / annual results filings.
        period: 'Quarterly' | 'Annual' | 'Six Month' | 'Others'
        """
        to_date   = to_date   or date.today()
        from_date = from_date or (to_date - timedelta(days=90))
        params: dict = {
            "index":  "equities",
            "period": period,
            "from":   _fmt(from_date),
            "to":     _fmt(to_date),
        }
        if symbol:
            params["symbol"] = symbol.upper()
        resp = self._session.get("corporates-financial-results", params=params)
        return resp if isinstance(resp, list) else resp.get("data", resp)

    def financial_results_detail(
        self,
        symbol:      str,
        consolidated: bool | None = None,
    ) -> list[dict]:
        """
        Structured quarterly P&L for a stock, parsed from NSE Integrated Filing XBRL.
        Returns one dict per quarter, newest first, with fields in ₹ Crores:

          symbol, company, quarter, period_start, period_end, type,
          revenue_from_operations, other_income, total_income,
          employee_costs, finance_costs, depreciation, other_expenses, total_expenses,
          ebit, pbt, tax, pat,
          eps_basic, eps_diluted

        consolidated=None (default): auto-detect — prefers Consolidated, falls back to Standalone.
        consolidated=True: Consolidated only. consolidated=False: Standalone only.
        Covers FY27 and onwards (integrated-filing-results endpoint).
        For older data use financial_results() + xbrl field.
        """
        resp = self._session.get(
            "integrated-filing-results",
            params={"symbol": symbol.upper(), "index": "equities"},
        )
        filings = resp.get("data", []) if isinstance(resp, dict) else resp

        if consolidated is None:
            available = {
                f.get("consolidated", "").strip()
                for f in filings
                if f.get("type") == "Integrated Filing- Financials"
            }
            nature = "Consolidated" if "Consolidated" in available else "Standalone"
        else:
            nature = "Consolidated" if consolidated else "Standalone"

        results = []

        for filing in filings:
            if filing.get("type") != "Integrated Filing- Financials":
                continue
            if filing.get("consolidated", "").strip() != nature:
                continue
            xbrl_url = filing.get("xbrl", "")
            if not xbrl_url or xbrl_url.endswith("-"):
                continue

            try:
                r = self._session._s.get(xbrl_url, timeout=15)
                r.raise_for_status()
                xbrl = _parse_xbrl(r.content)
            except Exception:
                continue

            qe = filing.get("qe_Date", "")
            g = xbrl.get
            ebit = (
                round(xbrl["Income"] - xbrl["Expenses"] + (g("FinanceCosts") or 0), 2)
                if g("Income") and g("Expenses") else None
            )
            results.append({
                # ── identity ──────────────────────────────────────────────
                "symbol":       g("Symbol", symbol.upper()),
                "company":      g("NameOfTheCompany", ""),
                "quarter":      g("ReportingQuarter", ""),
                "period_start": g("DateOfStartOfReportingPeriod", ""),
                "period_end":   g("DateOfEndOfReportingPeriod", qe),
                "type":         nature,
                "audited":      g("WhetherResultsAreAuditedOrUnaudited", ""),
                # ── P&L (₹ Cr) ────────────────────────────────────────────
                "revenue_from_operations": g("RevenueFromOperations"),
                "other_income":            g("OtherIncome"),
                "total_income":            g("Income"),
                "employee_costs":          g("EmployeeBenefitExpense"),
                "finance_costs":           g("FinanceCosts"),
                "depreciation":            g("DepreciationDepletionAndAmortisationExpense"),
                "other_expenses":          g("OtherExpenses"),
                "total_expenses":          g("Expenses"),
                "exceptional_items":       g("ExceptionalItems"),
                "ebit":                    ebit,
                "pbt":                     g("ProfitBeforeTax"),
                "tax":                     g("TaxExpense"),
                "pat":                     g("ProfitLossForPeriod"),
                "eps_basic":   g("BasicEarningsLossPerShareFromContinuingOperations")
                               or g("BasicEarningsLossPerShareFromTotalOperations"),
                "eps_diluted": g("DilutedEarningsLossPerShareFromContinuingOperations")
                               or g("DilutedEarningsLossPerShareFromTotalOperations"),
                # ── OCI & Total Comprehensive Income (₹ Cr) ───────────────
                "oci": g("OtherComprehensiveIncome"),
                "total_comprehensive_income": g("ComprehensiveIncomeForThePeriod"),
                # ── Balance Sheet (₹ Cr) ───────────────────────────────────
                "balance_sheet": {
                    "total_assets":            g("Assets"),
                    "current_assets":          g("CurrentAssets"),
                    "noncurrent_assets":       g("NoncurrentAssets"),
                    "cash":                    g("CashAndCashEquivalents"),
                    "bank_balances":           g("BankBalanceOtherThanCashAndCashEquivalents"),
                    "current_investments":     g("CurrentInvestments"),
                    "noncurrent_investments":  g("NoncurrentInvestments"),
                    "trade_receivables":       g("TradeReceivablesCurrent"),
                    "loans":                   (g("LoansCurrent") or 0) + (g("LoansNoncurrent") or 0) or None,
                    "inventories":             g("Inventories"),
                    "ppe":                     g("PropertyPlantAndEquipment"),
                    "capital_wip":             g("CapitalWorkInProgress"),
                    "goodwill":                g("Goodwill"),
                    "intangibles":             g("OtherIntangibleAssets"),
                    "deferred_tax_assets":     g("DeferredTaxAssetsNet"),
                    "equity":                  g("Equity"),
                    "equity_share_capital":    g("EquityShareCapital"),
                    "other_equity":            g("OtherEquity"),
                    "non_controlling_interest":g("NonControllingInterest"),
                    "total_liabilities":       g("Liabilities"),
                    "current_liabilities":     g("CurrentLiabilities"),
                    "noncurrent_liabilities":  g("NoncurrentLiabilities"),
                    "borrowings_current":      g("BorrowingsCurrent"),
                    "borrowings_noncurrent":   g("BorrowingsNoncurrent"),
                    "trade_payables":          g("TradePayablesCurrent"),
                    "other_current_liabilities":   g("OtherCurrentLiabilities"),
                    "other_noncurrent_liabilities":g("OtherNoncurrentLiabilities"),
                    "provisions_current":      g("ProvisionsCurrent"),
                    "provisions_noncurrent":   g("ProvisionsNoncurrent"),
                    "deferred_tax_liabilities":g("DeferredTaxLiabilitiesNet"),
                },
                # ── Cash Flow (₹ Cr) ──────────────────────────────────────
                "cash_flow": {
                    "operating":       g("CashFlowsFromUsedInOperatingActivities"),
                    "investing":       g("CashFlowsFromUsedInInvestingActivities"),
                    "financing":       g("CashFlowsFromUsedInFinancingActivities"),
                    "net_change":      g("IncreaseDecreaseInCashAndCashEquivalents"),
                    "fx_effect":       g("EffectOfExchangeRateChangesOnCashAndCashEquivalents"),
                    "closing_cash":    g("CashAndCashEquivalentsCashFlowStatement"),
                    "dividends_paid":  g("DividendsPaidClassifiedAsFinancingActivities"),
                    "capex":           g("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
                    "acquisitions":    g("CashFlowsUsedInObtainingControlOfSubsidiariesOrOtherBusinessesClassifiedAsInvestingActivities"),
                    "tax_paid":        g("IncomeTaxesPaidRefundClassifiedAsOperatingActivities"),
                    "interest_received": g("InterestReceivedClassifiedAsInvestingActivities"),
                    "lease_payments":  g("PaymentsOfLeaseLiabilitiesClassifiedAsFinancingActivities"),
                    "buyback":         g("PaymentsToAcquireOrRedeemEntitysShares"),
                    "debt_repaid":     g("RepaymentsOfBorrowingsClassifiedAsFinancingActivities"),
                },
                # ── Segments (₹ Cr) ───────────────────────────────────────
                "segments": {
                    "is_multi_segment":      g("IsCompanyReportingMultisegmentOrSingleSegment"),
                    "segment_name":          g("DescriptionOfReportableSegment"),
                    "revenue":               g("SegmentRevenue") or g("SegmentRevenueFromOperations"),
                    "profit_before_tax":     g("SegmentProfitBeforeTax"),
                    "ebit":                  g("SegmentProfitLossBeforeTaxAndFinanceCosts"),
                    "finance_costs":         g("SegmentFinanceCosts"),
                    "assets":                g("SegmentAssets"),
                    "liabilities":           g("SegmentLiabilities"),
                    "unallocated_expense_net": g("OtherUnallocableExpenditureNetOffUnAllocableIncome"),
                    "inter_segment_revenue": g("InterSegmentRevenue"),
                },
                # ── Attribution (₹ Cr) ────────────────────────────────────
                "attribution": {
                    "pat_to_owners": g("ProfitOrLossAttributableToOwnersOfParent"),
                    "pat_to_nci":    g("ProfitOrLossAttributableToNonControllingInterests"),
                    "tci_to_owners": g("ComprehensiveIncomeForThePeriodAttributableToOwnersOfParent"),
                    "tci_to_nci":    g("ComprehensiveIncomeForThePeriodAttributableToOwnersOfParentNonControllingInterests"),
                },
                # ── Meta ──────────────────────────────────────────────────
                "meta": {
                    "board_meeting_date": g("DateOfBoardMeetingWhenFinancialResultsWereApproved"),
                    "fy_start":           g("DateOfStartOfFinancialYear"),
                    "fy_end":             g("DateOfEndOfFinancialYear"),
                    "auditor":            g("AuditorsFirmName"),
                    "isin":               g("ISIN"),
                    "scrip_code":         g("ScripCode"),
                    "face_value":         g("FaceValueOfEquityShareCapital"),
                    "cash_flow_type":     g("TypeOfCashFlowStatement"),
                },
            })

        return results

    def financial_results_links(
        self,
        symbol:    str | None = None,
        period:    str = "Quarterly",
        from_date: date | None = None,
        to_date:   date | None = None,
    ) -> list[dict]:
        """
        NSE financial results with the Details HTML page URL and XBRL URL.

        The 'details_url' opens the NSE financial results HTML page
        (same as the 'Details' link on the NSE website).
        The 'xbrl_url' is the raw Ind-AS XBRL XML file.

        Returns list of dicts: symbol, company, quarter, period_end,
          consolidated, audited, broadcast_time, details_url, xbrl_url.
        """
        _BASE = "https://www.nseindia.com/companies-listing/corporate-filings-financial-results"
        raw = self.financial_results(
            symbol=symbol, period=period,
            from_date=from_date, to_date=to_date,
        )
        out = []
        for r in raw:
            params = r.get("params", "")
            out.append({
                "symbol":         r.get("symbol", ""),
                "company":        r.get("companyName", ""),
                "quarter":        r.get("relatingTo", ""),
                "period_end":     r.get("toDate", ""),
                "consolidated":   r.get("consolidated", ""),
                "audited":        r.get("audited", ""),
                "broadcast_time": r.get("broadCastDate", ""),
                "details_url":    f"{_BASE}?params={params}" if params else "",
                "xbrl_url":       r.get("xbrl", ""),
            })
        return out

    def integrated_filing_rss(
        self,
        from_date: date | None = None,
        to_date:   date | None = None,
        symbol:    str | None = None,
    ) -> list[dict]:
        """
        Integrated Filing – Financials with broadcast timestamp.

        Each record has: symbol, company, broadcast_time, filing_type,
        consolidated, xbrl_url.

        - No args → latest 20 filings from the NSE RSS feed (real-time, fastest).
        - from_date / to_date → historical results via financial_results()
          (supports any date range, returns broadCastDate from NSE filings).
        - symbol → filter to one company (works with both modes).
        """
        import xml.etree.ElementTree as ET

        _RSS = "https://nsearchives.nseindia.com/content/RSS/Integrated_Filing_Financials.xml"

        if from_date is None and to_date is None:
            # ── Live RSS feed (last 20) ──────────────────────────────────
            try:
                r = self._session._s.get(_RSS, timeout=10)
                r.raise_for_status()
                root = ET.fromstring(r.text)
                out = []
                for item in root.findall(".//item"):
                    title   = item.findtext("title", "").strip()
                    link    = item.findtext("link", "").strip()
                    desc    = item.findtext("description", "").strip()
                    pubdate = item.findtext("pubDate", "").strip()
                    # desc format: "Integrated Filing- Financials|Original|"
                    parts = [p.strip() for p in desc.split("|")]
                    filing_type  = parts[1] if len(parts) > 1 else ""
                    consolidated = parts[2] if len(parts) > 2 else ""
                    sym = link.split("_")[2] if "_" in link else ""
                    rec = {
                        "symbol":        sym,
                        "company":       title,
                        "broadcast_time": pubdate,
                        "filing_type":   filing_type,
                        "consolidated":  consolidated,
                        "xbrl_url":      link,
                    }
                    if symbol and rec["company"].lower().find(symbol.lower()) == -1:
                        continue
                    out.append(rec)
                return out
            except Exception:
                pass  # fall through to financial_results if RSS fails

        # ── Historical via financial_results() ──────────────────────────
        raw = self.financial_results(
            symbol=symbol,
            from_date=from_date,
            to_date=to_date,
        )
        out = []
        for r in raw:
            xbrl = r.get("xbrl", "") or ""
            if not xbrl or not xbrl.startswith("http"):
                continue
            out.append({
                "symbol":         r.get("symbol", ""),
                "company":        r.get("companyName", ""),
                "broadcast_time": r.get("broadCastDate", r.get("exchdisstime", "")),
                "filing_type":    "Original" if r.get("oldNewFlag") == "N" else "Revised",
                "consolidated":   r.get("consolidated", ""),
                "xbrl_url":       xbrl,
            })
        return out

    def shareholding_patterns(
        self,
        symbol: str,
        from_date: date | None = None,
        to_date:   date | None = None,
    ) -> list[dict]:
        """
        Quarterly shareholding pattern (promoter / FII / DII / public).
        """
        resp = self._session.get(
            "top-corp-info",
            params={"symbol": symbol.upper(), "market": "equities"},
        )
        shp = resp.get("shareholdings_patterns", {}) if isinstance(resp, dict) else {}
        data = shp.get("data", shp.get("shareholding", [])) if isinstance(shp, dict) else shp
        # NSE returns data as a dict keyed by quarter-end date, e.g. {"31-Dec-2025": [...]}
        if isinstance(data, dict):
            rows = []
            for quarter_date, entries in data.items():
                if isinstance(entries, list):
                    for entry in entries:
                        rows.append({"date": quarter_date, **entry})
                else:
                    rows.append({"date": quarter_date, "data": entries})
            return rows
        return data if isinstance(data, list) else []

    def annual_reports(self, symbol: str) -> list[dict]:
        """List of annual report PDFs for a company."""
        resp = self._session.get(
            "annual-reports",
            params={"symbol": symbol.upper(), "index": "cm"},
        )
        return resp if isinstance(resp, list) else resp.get("data", resp)

    def insider_trading(
        self,
        symbol:    str | None = None,
        from_date: date | None = None,
        to_date:   date | None = None,
    ) -> list[dict]:
        """
        SEBI insider trading disclosures (SAST / PIT).
        Pass a symbol for best results — NSE returns empty without one
        unless there happen to be bulk market-wide disclosures in the window.
        Default window is 90 days.
        """
        to_date   = to_date   or date.today()
        from_date = from_date or (to_date - timedelta(days=90))
        params: dict = {
            "from": _fmt(from_date),
            "to":   _fmt(to_date),
        }
        if symbol:
            params["symbol"] = symbol.upper()
        resp = self._session.get("corporates-pit", params=params)
        return resp if isinstance(resp, list) else resp.get("data", resp)

    def event_calendar(self) -> list[dict]:
        """
        Comprehensive upcoming corporate event calendar from NSE.
        Returns all companies with scheduled board meetings, results announcements,
        dividends, AGMs, etc. across the current results season (~19 dates, ~490 entries).
        Each record has: symbol, companyName, purpose, bm_date (dd-MMM-YYYY).
        Unlike board_meetings(), this endpoint returns the full forward-looking calendar
        rather than only the 20 most-recently-filed meeting notices.
        """
        resp = self._session.get("event-calendar")
        return resp if isinstance(resp, list) else resp.get("data", resp)

    def corporate_pledge_data(self, symbol=None, from_date=None, to_date=None, index='equities'):
        """Promoter shares pledge data."""
        params = {'index': index}
        if symbol:
            params['symbol'] = symbol.upper()
        if from_date:
            params['from_date'] = from_date
        if to_date:
            params['to_date'] = to_date
        return (self._guardian.guarded_get if self._guardian else self._session.get)('corporate-pledgedata', params)

    def corporate_voting_results(self, symbol=None, from_date=None, to_date=None, index='equities'):
        """Shareholder voting results."""
        params = {'index': index}
        if symbol:
            params['symbol'] = symbol.upper()
        if from_date:
            params['from_date'] = from_date
        if to_date:
            params['to_date'] = to_date
        return (self._guardian.guarded_get if self._guardian else self._session.get)('corporate-voting-results', params)

    def corporate_actions_rights(self, symbol=None, from_date=None, to_date=None):
        """Rights issue corporate actions."""
        params = {'index': 'equities', 'subject': 'rights'}
        if symbol:
            params['symbol'] = symbol.upper()
        if from_date:
            params['from_date'] = from_date
        if to_date:
            params['to_date'] = to_date
        return (self._guardian.guarded_get if self._guardian else self._session.get)('corporates-corporateActions', params)

    def insider_trading_pit(self, symbol=None, from_date=None, to_date=None):
        """Insider trading disclosures (SEBI PIT)."""
        params = {'index': 'equities'}
        if symbol:
            params['symbol'] = symbol.upper()
        if from_date:
            params['from_date'] = from_date
        if to_date:
            params['to_date'] = to_date
        return (self._guardian.guarded_get if self._guardian else self._session.get)('corporates-pit', params)

    def sast_reg29(self, symbol=None, from_date=None, to_date=None, index='equities'):
        """SAST Regulation 29 substantial acquisition disclosures."""
        params = {'index': index}
        if symbol:
            params['symbol'] = symbol.upper()
        if from_date:
            params['from_date'] = from_date
        if to_date:
            params['to_date'] = to_date
        return (self._guardian.guarded_get if self._guardian else self._session.get)('corporate-sast-reg29', params)

    def credit_ratings(
        self,
        symbol: str | None = None,
        isin:   str | None = None,
    ) -> list[dict]:
        """
        Credit rating disclosures filed with NSE.
        Returns all recent filings — NSE does not support server-side filtering,
        so pass symbol or isin to filter client-side.

        Each record has: Symbol, CompanyName, ISIN, NameOfCRAgency, CreditRating,
        RatingAction, Outlook, DateofCR, CreditRatingEarlier, OutlookEarlier,
        DateOfCREarlier, BroadcastDateTime, XbrlFileName.
        """
        resp = self._session.get("corporate-credit-rating")
        data = resp if isinstance(resp, list) else resp.get("data", [])
        if symbol:
            sym = symbol.upper()
            data = [r for r in data
                    if (r.get("Symbol") or "").upper() == sym
                    or sym in (r.get("CompanyName") or "").upper()]
        if isin:
            data = [r for r in data if r.get("ISIN") == isin or r.get("ISIN_ER") == isin]
        return data

    def large_deals_snapshot(self):
        """Fetch the capital market large deals snapshot (bulk, block and short selling deals)."""
        path = "snapshot-capital-market-largedeal"
        params = {}
        return (self._guardian.guarded_get if self._guardian else self._session.get)(path, params)
