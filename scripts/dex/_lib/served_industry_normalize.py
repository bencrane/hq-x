"""Rules-based normalizer for served-industry strings.

Maps free-text industry names (scraped, un-normalized) to a fixed set of
canonical segments. Ordered keyword rules, first match wins:
  specific verticals  ->  not-industry (lender products / stage descriptors)
  ->  horizontals  ->  Other.

v2: expanded for the prospeo small-business / MCA vocabulary (restaurants by
cuisine, medical practices, trades, salons) on top of the SFNet institutional
ABL vocabulary; adds the Personal & Consumer Services segment.

Run:  python3 normalize_industries.py <raw_csv> <out_csv>
"""
from __future__ import annotations
import csv, sys

__version__ = "2.0.0"

# (canonical_segment, is_real_industry, [keywords]) -- ordered, first match wins.
RULES: list[tuple[str, bool, list[str]]] = [
    # ===================== specific verticals ============================
    ("Oil & Gas", True, [
        "oil", "gas", "oilfield", "petroleum", "coal", "lng", "fuel service",
        "propane", "gasoline"]),
    ("Aerospace & Defense", True, [
        "aerospace", "defense", "defence", "aviation", "aircraft", "military",
        "ammunition", "shipyard", "drydock", "marine equipment", "firearm",
        "gun/rifle", "guns & ammo", "gun shop"]),
    ("Healthcare & Life Sciences", True, [
        "health", "medical", "pharma", "biotech", "biopharma", "life science",
        "hospital", "nursing", "clinic", "physician", "dental", "dentist",
        "therap", "hospice", "infusion", "diagnostic", "medtech", "senior care",
        "senior housing", "assisted living", "urgent care", "rehabilitation",
        "memory care", "acos", "ipas", "msos", "drug discovery", "pharmac",
        "behavioral health", "substance use", "respiratory", "veterinar",
        "skilled nursing", "home care", "home health", "outpatient",
        "ambulatory", "post-acute", "sciences", "therapeutic", "laborator",
        "independent living", "adult residence", "chiropract", "dermatolog",
        "cardiolog", "neurolog", "neurosurg", "surgeon", "surgery", "pediatric",
        "orthodont", "periodont", "osteopath", "optometr", "optician",
        "ophthalm", "podiatr", "psychiatr", "psycholog", "ob/gyn", "obstetric",
        "gynecolog", "internist", "internal medicine", "family practice",
        "family practitioner", "general practitioner", "general dentistry",
        "naturopath", "acupunctur", "reflexology", "doctors", "md’s", "md's",
        "ologists", "ear nose", "maxillofacial", "aesthetic laser",
        "acne treatment", "laser hair removal", "weight loss", "med spa",
        "medspa", "blood & plasma", "dialysis", "imaging", "anesthe",
        "radiolog", "pathology", "wellness", "vitamins & supplement",
        "practice group", "professional practice", "care home", "day care",
        "child care", "nursery care", "biotechnology"]),
    ("Staffing & Recruiting", True, [
        "staffing", "recruit", "employment agenc", "temp staffing",
        "temporary staffing", "temporary employ", "labour hire", "labor hire",
        "human resource", "payroll", "business process outsourc", "call center",
        "freelance worker", "temp agenc"]),
    ("Apparel & Textiles", True, [
        "apparel", "textile", "fashion", "clothing", "footwear", "shoe",
        "lingerie", "sweater", "handbag", "garment", "denim", "jewelry",
        "jewlery", "leisurewear", "women's accessor", "women’s accessor",
        "swimwear", "bridal", "embroidery", "leather goods", "wigs",
        "knitting", "hats", "costumes", "eyewear"]),
    ("Automotive", True, [
        "automotive", "auto repair", "auto detail", "auto body", "auto part",
        "auto custom", "auto upholstery", "car wash", "car rental", "car broker",
        "car stereo", "car window", "car inspect", "body shop", "tow ", "towing",
        "transmission repair", "motorcycle", "tire", "trailers and campers",
        "diesel engine", "auto dealer", "auto deal", "smog check",
        "wheel & rim", "windshield", "vehicle", "junkyard", "motor coach"]),
    ("Metals & Mining", True, [
        "metal", "mining", "minerals", "steel", "foundry", "machining",
        "machine shop", "machine tool", "cnc", "metalwork", "tubular",
        "aggregate"]),
    ("Chemicals", True, [
        "chemical", "plastic", "paint", "coating", "water purification",
        "water treatment & filtration"]),
    ("Paper, Packaging & Printing", True, [
        "paper", "packaging", "printing", "envelope", "corrugat", "pulp",
        "graphic art", "reprographic", "vellum", "folding and set-up",
        "signmaking", "signage", "print "]),
    ("Agriculture", True, [
        "agricultur", "agribusiness", "farm", "cotton", "produce distributor",
        "paca", "protein", "rural", "natural products", "forestry", "logging",
        "lumber", "cannabis", "cannabusiness", "grow operation", "cattle",
        "poultry", "almond", "macadamia", "vineyard", "cropping", "arbor",
        "nurseries & garden", "christmas tree", "firewood"]),
    # ----- food: restaurants/bars (Hospitality) before food-product ------
    ("Hospitality & Restaurants", True, [
        "restaurant", "hotel", "motel", "lodging", "hospitality", "travel",
        "accommodation", "food service", "foodservice", "cafe", "cafeteria",
        "diner", "bistro", "bar ", "bars", "pub", "brewpub", "nightclub",
        "tavern", "lounge", "eatery", "eating place", "eating & drinking",
        "catering", "caterer", "bakery", "bakeries", "pizz", "burger",
        "sandwich", "deli", "steakhouse", "sushi", "barbeque", "bbq", "grill",
        "buffet", "coffee", "tea room", "juice bar", "smoothie", "ice cream",
        "frozen yogurt", "donut", "bagel", "cupcake", "dessert", "chocolat",
        "creper", "patisserie", "gelato", "macaron", "candy", "snack",
        "noodle", "ramen", "hot pot", "dim sum", "tacos", "taco ", "waffle",
        "pancake", "soup", "salad", "wrap", "poke", "kebab", "falafel",
        "empanada", "burrito", "fondue", "tapas", "izakaya", "teppanyaki",
        "speakeasy", "brewer", "brewpub", "winery", "distiller", "vineyard",
        "resort", "hostel", "bed & breakfast", "campground", "rv park",
        "tourism", "turismo", "vacation rental", "supper club", "tiki bar",
        "cocktail", "whiskey bar", "wine bar", "piano bar", "gay bar",
        "dive bar", "cigar bar", "hookah", "karaoke", "club crawl", "venue",
        # cuisines
        "afghan", "african", "american (", "argentine", "asian", "australian",
        "brazilian", "british", "burmese", "cajun", "cantonese", "caribbean",
        "chinese", "colombian", "cuban", "dominican", "ethiopian", "filipino",
        "french", "german", "greek", "halal", "hawaiian", "himalayan",
        "indian", "irish", "italian", "japanese", "korean", "kosher",
        "laotian", "latin american", "lebanese", "malaysian", "mediterranean",
        "mexican", "middle eastern", "modern european", "mongolian",
        "moroccan", "pakistani", "persian", "iranian", "peruvian", "polish",
        "portuguese", "puerto rican", "russian", "salvadoran", "shanghainese",
        "sicilian", "southern", "szechuan", "taiwanese", "tex-mex", "thai",
        "turkish", "tuscan", "venezuelan", "vietnamese", "spanish",
        "chicken shop", "chicken wing", "cheesesteak", "fish & chips",
        "smokehouse", "butcher", "meat shop", "cheese shop", "street vendor",
        "food truck", "personal chef", "vegan", "vegetarian", "gluten-free",
        "bubble tea", "themed cafe", "dining"]),
    ("Food & Beverage", True, [
        "food processing", "packaged food", "food manufactur", "grocery",
        "food & beverage", "food and beverage", "food & agribusiness",
        "food and agribusiness", "food and agriculture", "food supply",
        "food related", "beverage", "alcoholic", "dairy", "seafood",
        "produce", "fruits & veggies", "herbs & spices", "organic store",
        "natural food", "foodservice equipment", "food &", "food/", "food,"]),
    ("Technology & Software", True, [
        "technology", "software", "saas", "electronic", "semiconductor",
        "semi-conductor", "computer", "fintech", "internet", "data center",
        "robotics", "automation", "high tech", "high-tech", "hightech",
        "information technolog", "it consult", "it service", "it provider",
        "it solution", "it/tech", "it / ", "it & eng", "enterprise software",
        "cleantech", "tech-enabled", "tech enabled", "digital infrastructure",
        "networking", "artificial intelligence", "ai-related", "ai-enabled",
        "intelligent application", "marketing technology", "ad tech",
        "payment technology", "battery technolog", "data and analytic",
        "sustainable tech", "tmt", "mobile phone", "it companies", "it "]),
    ("Marketing & Advertising", True, ["advertising", "marketing"]),
    ("Entertainment, Gaming & Sports", True, [
        "gaming", "casino", "entertainment", "sports", "music", "leisure",
        "recreation", "arts, entertainment", "amusement", "arcade", "bowling",
        "boxing", "golf", "gym", "fitness", "yoga", "pilates", "barre",
        "martial art", "jiu-jitsu", "gymnastics", "tennis", "soccer",
        "basketball", "batting cage", "axe throwing", "escape game",
        "escape room", "laser tag", "mini golf", "go kart", "go-kart",
        "trampoline", "skating rink", "ski resort", "ski & snowboard",
        "water park", "theme park", "haunted house", "comedy club",
        "dance club", "dance studio", "dance hall", "nightlife", "festival",
        "concert", "stadium", "arena", "race track", "racetrack", "country club",
        "museum", "art galler", "botanical garden", "zoo", "aquarium",
        "observator", "playground", "play centre", "playcentre", "summer camp",
        "theater", "theatre", "cinema", "performing arts", "film, play",
        "studio taping", "dj", "djs", "virtual reality", "tabletop game",
        "video game", "marina", "boat charter", "boat tour", "horseback",
        "hiking", "rafting", "kayak", "hunt", "bingo", "pool hall",
        "billiard", "scavenger hunt", "trivia", "party bus", "limo",
        "event space", "event rental", "event planning", "event planner",
        "party & event", "party supplies", "party character", "wedding",
        "social club"]),
    ("Media & Telecommunications", True, [
        "telecom", "media", "publishing", "publisher", "broadcast", "cable",
        "cellular", "wireless", "television", "communications", "radio station",
        "newspaper", "vinyl record", "comic book"]),
    ("Energy & Power", True, [
        "energy", "power", "utilit", "renewable", "solar", "ev charging",
        "ev and charging", "natural resources", "battery"]),
    ("Government & Public Sector", True, [
        "government", "governament", "municipal", "public sector",
        "public administration", "public finance", "gov con", "federal",
        "public private", "public-private", "state agenc", "police",
        "fire department", "fire house", "firehouse", "post office",
        "usda", "libraries", "library", "public market"]),
    ("Construction", True, [
        "construction", "construcction", "contractor", "subcontractor",
        "building product", "building material", "building supplies",
        "building & maintenance", "homebuild", "home builder", "builder",
        "civil engineering", "hvac", "hvacr", "landscaping", "landscape",
        "demolition", "crane", "rigging", "roofing", "concrete", "yellow iron",
        "infrastructure", "electrical", "electrician", "plumbing", "plumber",
        "flooring", "tiling", "tile", "cabinet", "framing", "fences & gates",
        "fencing", "windows install", "window install", "countertop",
        "kitchen & bath", "glass & mirror", "stone, glass", "shades & blinds",
        "handyman", "home improvement", "ground up", "septic", "spray foam",
        "insulation", "home inspect", "homeinspect", "excavat", "paving",
        "masonry", "drywall", "carpentry", "remodel", "renovation",
        "engineering & construction", "engineers", "architect"]),
    ("Real Estate", True, [
        "real estate", "real-estate", "realtor", "multifamily", "multi-family",
        "multi family", "housing", "homeowner", "condo", "propert", "reit",
        "land develop", "apartment", "office building", "office buildings",
        "mixed use", "mixed-use", "shopping cent", "mobile home park",
        "single tenant", "single-purpose building", "residential complex",
        "investment real estate", "estate agent", "developers", "developer"]),
    ("Education", True, [
        "education", "school", "university", "universities", "college",
        "higher education", "academic", "tutoring", "summer camp"]),
    ("Nonprofit & Associations", True, [
        "nonprofit", "non-profit", "not for profit", "not-for-profit",
        "association", "religious", "foundation", "endowment", "church",
        "house of worship", "ngo", "charit"]),
    ("Environmental & Waste Services", True, [
        "waste", "recycling", "environment", "remediation", "water treatment",
        "disaster", "dumpster", "junk removal", "pest control"]),
    ("Security Services", True, [
        "security guard", "security firm", "security alarm", "security service",
        "security industry", "security accounts", "security alarm", "locksmith",
        "security"]),
    ("Facilities & Janitorial Services", True, [
        "janitor", "facility maintenance", "facilities", "cleaning",
        "building maintenance", "dry clean", "laundromat", "laundry",
        "pool clean", "chimney sweep", "car detailing"]),
    ("Personal & Consumer Services", True, [
        "salon", "spa", "barber", "hair stylist", "hair extension",
        "hair removal", "nail ", "nail technician", "massage", "tanning",
        "tattoo", "piercing", "waxing", "makeup", "cosmetolog", "esthetic",
        "permanent makeup", "beauty", "funeral", "mortician", "photograph",
        "pet ", "pet food", "pet care", "veterinarian", "wedding planning",
        "sewing & alteration", "personal trainer", "trainers", "fitness",
        "spray tanning", "saunas", "day spa", "watch repair", "shoe repair"]),
    ("Consumer Products", True, [
        "consumer product", "consumer goods", "consumer staples", "cpg",
        "consumer packaged", "toys", "giftware", "gift shop", "gifts",
        "furniture", "home goods", "home furnishing", "home decor",
        "housewares", "household", "sporting goods", "promotional product",
        "recreational product", "appliance", "personal care", "luxury",
        "baby product", "baby accessor", "craft", "school supplies",
        "mattress", "cosmetic", "books", "stationery", "musical instrument",
        "outdoor gear", "hardware store"]),
    # ===================== NOT an industry ===============================
    ("(not an industry)", False, [
        "asset-based lend", "asset based lend", "asset-backed lend", "abl ",
        "abl&", "abl &", " abl", "abl lender", "factoring", "factors",
        "invoice factor", "invoice financ", "specialty finance",
        "commercial finance", "commodity finance", "fund finance",
        "fund banking", "fund services", "lender finance", "private credit",
        "venture capital", "mortgage", "channel finance", "dealer finance",
        "dealer financial", "leverage finance", "sponsor", "purchase order",
        "po financing", "inventory lend", "inventory financ", "inventory finance",
        "bridge lend", "bridge loan", "bridge & hard", "hard money",
        "equipment lend", "saas lend", "lenders", "lender ", "corporate credit",
        "corporate trust", "capital markets", "private equity", "private money",
        "asset management", "asset manager", "wealth management",
        "broker-dealer", "broker dealer", "futures and securities", "exchanges",
        "money service", "correspondent banking", "corporate banking",
        "international banking", "premier banking", "middle market banking",
        "middle market lend", "institutional", "alternative finance",
        "alternative asset", "traditional asset", "commercial lending",
        "consumer lending", "consumer finance", "practice finance",
        "tax credit", "leasing", "line of credit", "lines of credit",
        "working capital", "merchant cash", "mezzanine", "dscr", "fix & flip",
        "fix and flip", "sale leaseback", "sba loan", "sba term", "sba/specialty",
        "litigation funding", "ar funding", "acquisition funding", "m&a",
        "partner buyout", "interest only", "specialized lending",
        "currency exchange", "check cashing", "pay-day", "payday",
        "gold buyers", "vendor financing", "term loan", "real estate capital",
        "real-estate lending", "real estate lend", "construction loan",
        "construction financ", "commercial lending for", "campground financing",
        "auto repair shop financing", "bar & nightclub financing",
        "church financing", "specialty coffee financing", "franchise financ",
        "franchise banking", "foreign national", "estate liquidation"]),
    ("(not an industry)", False, [
        "small business", "small or medium", "small and medium",
        "small- and medium", "small to medium", "small-to-medium", "startup",
        "start-up", "start up", "middle market", "high growth", "high-revenue",
        "privately held", "private companies", "diversified industr",
        "all industries", "all businesses", "any industry", "every industry",
        "various industries", "innovation economy", "early stage",
        "women-led", "women led", "bootstrapped", "capital constrained",
        "select services companies", "niche", "b2b ", "b2b(",
        "business-to-business", "other capital", "acquisitions",
        "general trading", "general business", "israel gateway", "latam",
        "empresas con", "equipos de", "exportadores", "for companies operating",
        "highly seasonal", "high net worth", "commodities", "borrowers",
        "business owners", "established merchants", "qualifying entities",
        "national chain", "multi-location", "multiple-location",
        "neighborhood storefront", "local business", "commercial business",
        "everything else", "for founders", "for sales teams", "for revops",
        "for outbound", "for agencies", "for data licensing",
        "for large businesses", "for multiple", "founders", "investors",
        "corporate", "holding compan", "conglomerate", "organisations",
        "organizations", "businesses", "merchants", "entrepreneur", "sales",
        "support", "agencies", "seasonal business"]),
    # ===================== horizontals ===================================
    ("Retail & E-Commerce", True, [
        "retail", "e-commerce", "ecommerce", "e commerce", "e- commerce",
        "direct to consumer", "direct-to-consumer", "convenience store",
        "c-store", "c-stores", "online retail", "store", "shop", "drugstore",
        "pharmacies", "pharmacy", "liquor store", "pawn", "flea market",
        "department store", "outlet", "boutique", "thrift", "antique",
        "vape", "tobacco", "newsstand", "florist", "nurser", "dealership"]),
    ("Wholesale & Distribution", True, [
        "wholesale", "distribut", "import", "export", "trading", "reseller",
        "re-seller", "suppliers", "supplier", "trade buyer", "warehous"]),
    ("Transportation & Logistics", True, [
        "transport", "trucking", "truck", "freight", "logistic", "rail",
        "shipping", "fulfillment", "cargo", "hauling", "maritime", "marine",
        "3pl", "third party logistics", "fleet", "supply chain", "storage",
        "courier", "messenger", "movers", "moving", "taxi", "livery",
        "shuttle", "parking", "airport", "train station", "delivery"]),
    ("Financial Services", True, [
        "financial institution", "financial services", "bank", "insurance",
        "credit union", "financials", "finance and insurance", "fintech",
        "accounting", "accountant", "cpa", "bookkeeping", "notar"]),
    ("Manufacturing", True, [
        "manufactur", "industrial", "factory", "fabricat", "production",
        "processing", "assembly", "extruding", "molding", "woodworking",
        "machinery", "equipment", "material", "refrigerat", "pallet",
        "lighting", "hydraulic", "industries"]),
    ("Business & Professional Services", True, [
        "business service", "professional service", "consulting", "consultant",
        "service provider", "service compan", "outsourc", "legal", "law firm",
        "law practice", "lawyer", "attorney", "advisory", "inspection",
        "administrative support", "engineering", "design", "services",
        "service"]),
    # Personal-services catch (after horizontals, before Other)
    ("Personal & Consumer Services", True, [
        "repair shop", "repair", "rental", "cleaners"]),
]


def normalize(raw: str) -> tuple[str, bool]:
    t = (raw or "").strip().lower()
    if not t:
        return ("Other", False)
    for segment, is_real, keywords in RULES:
        for kw in keywords:
            if kw in t:
                return (segment, is_real)
    return ("Other", True)


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "/tmp/raw_industries.csv"
    dst = sys.argv[2] if len(sys.argv) > 2 else "/tmp/industry_normalization.csv"
    rows = []
    with open(src, newline="") as f:
        for r in csv.DictReader(f):
            raw = r["raw_industry_name"]
            seg, is_real = normalize(raw)
            rows.append((raw, seg, is_real, int(r.get("companies", 1))))
    with open(dst, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["raw_industry_name", "canonical_segment", "is_real_industry"])
        for raw, seg, is_real, _ in rows:
            w.writerow([raw, "" if not is_real else seg, is_real])

    from collections import Counter
    seg_co, seg_strings = Counter(), Counter()
    for raw, seg, is_real, comp in rows:
        key = seg if is_real else "(not an industry)"
        seg_co[key] += comp
        seg_strings[key] += 1
    print(f"normalizer v{__version__} -- {len(rows)} distinct raw strings\n")
    print(f"{'segment':38s} {'raw_strings':>12s} {'company_hits':>13s}")
    for seg, _ in seg_co.most_common():
        print(f"  {seg:36s} {seg_strings[seg]:>12d} {seg_co[seg]:>13d}")
    other = sorted(raw for raw, seg, ir, _ in rows if ir and seg == "Other")
    print(f"\n--- {len(other)} strings still 'Other' ---")
    for o in other:
        print(f"  {o}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
