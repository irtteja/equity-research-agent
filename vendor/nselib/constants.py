from enum import Enum


class INDEX(str, Enum):
    NIFTY_50        = "NIFTY 50"
    NIFTY_BANK      = "NIFTY BANK"
    NIFTY_IT        = "NIFTY IT"
    NIFTY_FMCG      = "NIFTY FMCG"
    NIFTY_AUTO      = "NIFTY AUTO"
    NIFTY_PHARMA    = "NIFTY PHARMA"
    NIFTY_METAL     = "NIFTY METAL"
    NIFTY_ENERGY    = "NIFTY ENERGY"
    NIFTY_REALTY    = "NIFTY REALTY"
    NIFTY_MEDIA     = "NIFTY MEDIA"
    NIFTY_PSU_BANK  = "NIFTY PSU BANK"
    NIFTY_MIDCAP_50 = "NIFTY MIDCAP 50"
    NIFTY_MIDCAP_100= "NIFTY MIDCAP 100"
    NIFTY_SMALLCAP  = "NIFTY SMALLCAP 100"
    NIFTY_100       = "NIFTY 100"
    NIFTY_200       = "NIFTY 200"
    NIFTY_500       = "NIFTY 500"
    NIFTY_NEXT_50   = "NIFTY NEXT 50"
    NIFTY_FIN_SERVICE = "NIFTY FINANCIAL SERVICES"
    INDIA_VIX       = "INDIA VIX"
    NIFTY_FINNIFTY  = "NIFTY FIN SERVICE 25"
    SENSEX          = "S&P BSE SENSEX"


class SEGMENT(str, Enum):
    EQUITIES  = "equities"
    SME       = "sme"
    DEBT      = "debt"
    MF        = "mf"
    INVIT     = "invitsreits"


class SERIES(str, Enum):
    EQ  = "EQ"   # normal equity
    BE  = "BE"   # trade-to-trade
    BZ  = "BZ"
    SM  = "SM"   # SME
    ST  = "ST"
    SZ  = "SZ"


class INSTRUMENT(str, Enum):
    FUTIDX = "FUTIDX"
    FUTSTK = "FUTSTK"
    OPTIDX = "OPTIDX"
    OPTSTK = "OPTSTK"
    FUTIVX = "FUTIVX"


class OPTION_TYPE(str, Enum):
    CE = "CE"
    PE = "PE"


class DEAL_TYPE(str, Enum):
    BULK         = "bulk_deals"
    BLOCK        = "block_deals"
    SHORT_SELLING = "short_selling"


# Department codes for circulars
class DEPT(str, Enum):
    CAPITAL_MARKET   = "CMTR"
    FNO              = "FAO"
    CURRENCY         = "CD"
    SURVEILLANCE     = "SURV"
    LISTING          = "CMLS"
    IPO              = "IPO"
    MUTUAL_FUND      = "MF"
    INSPECTION       = "INSP"
    SME              = "SME"
    DEBT             = "DS"
    LEGAL            = "LEGL"
    SLBS             = "SLBS"
    MEMBERSHIP       = "MEMB"
    SYSTEMS          = "TEL"
    COMMODITY        = "COM"
    CORPORATE_COMM   = "CC"
    FINANCE          = "FAAC"
    WDM              = "WDTR"
