#!/bin/bash
# Test USASpending.gov and SAM.gov APIs via curl. Produces JSON output for report generation.
set -e
OUT_DIR="${1:-/tmp/api_test_output}"
mkdir -p "$OUT_DIR"
SAM_KEY="${SAM_GOV_API_KEY:-}"

echo "Output dir: $OUT_DIR"

# USASpending - no auth
echo "Test 1: USASpending spending_by_award..."
curl -s -X POST "https://api.usaspending.gov/api/v2/search/spending_by_award/" \
  -H "Content-Type: application/json" \
  -d '{"filters":{"award_type_codes":["A","B","C","D"],"naics_codes":{"require":["31","32","33"]},"recipient_type_names":["small_business"],"time_period":[{"start_date":"2026-02-13","end_date":"2026-03-13"}]},"fields":["Award ID","Recipient Name","recipient_id","Award Amount","Awarding Agency","NAICS","Start Date","Place of Performance State Code","generated_internal_id"],"sort":"Start Date","order":"desc","limit":10}' \
  > "$OUT_DIR/test1.json"

echo "Test 1b: page 2..."
curl -s -X POST "https://api.usaspending.gov/api/v2/search/spending_by_award/" \
  -H "Content-Type: application/json" \
  -d '{"filters":{"award_type_codes":["A","B","C","D"],"naics_codes":{"require":["31","32","33"]},"recipient_type_names":["small_business"],"time_period":[{"start_date":"2026-02-13","end_date":"2026-03-13"}]},"fields":["Award ID","Recipient Name","recipient_id","Start Date"],"sort":"Start Date","order":"desc","limit":10,"page":2}' \
  > "$OUT_DIR/test1b.json"

# Extract IDs from test1
RECIPIENT_ID=$(jq -r '.results[0].recipient_id // empty' "$OUT_DIR/test1.json")
GEN_ID=$(jq -r '.results[0].generated_internal_id // empty' "$OUT_DIR/test1.json")

echo "Test 2: Recipient $RECIPIENT_ID..."
curl -s "https://api.usaspending.gov/api/v2/recipient/${RECIPIENT_ID}/" > "$OUT_DIR/test2.json"

echo "Test 3: Award $GEN_ID..."
curl -s "https://api.usaspending.gov/api/v2/awards/${GEN_ID}/" > "$OUT_DIR/test3.json"

echo "Test 4: NAICS reference..."
curl -s "https://api.usaspending.gov/api/v2/references/naics/33/" > "$OUT_DIR/test4.json"

echo "Test 5: Bulk download..."
curl -s -X POST "https://api.usaspending.gov/api/v2/bulk_download/awards/" \
  -H "Content-Type: application/json" \
  -d '{"filters":{"prime_award_types":["A","B","C","D"],"date_type":"action_date","date_range":{"start_date":"2026-03-01","end_date":"2026-03-08"},"agencies":[{"type":"awarding","tier":"toptier","name":"Department of Defense"}]}}' \
  > "$OUT_DIR/test5.json"

echo "Test 6: Full fields..."
curl -s -X POST "https://api.usaspending.gov/api/v2/search/spending_by_award/" \
  -H "Content-Type: application/json" \
  -d '{"filters":{"award_type_codes":["A","B","C","D"],"naics_codes":{"require":["31","32","33"]},"recipient_type_names":["small_business"],"time_period":[{"start_date":"2026-02-13","end_date":"2026-03-13"}]},"fields":["Award ID","Recipient Name","Recipient DUNS Number","recipient_id","Recipient UEI","Awarding Agency","Awarding Sub Agency","Funding Agency","Funding Sub Agency","Place of Performance State Code","Start Date","End Date","Award Amount","Total Outlays","Contract Award Type","NAICS","PSC","Recipient Location","Primary Place of Performance","generated_internal_id"],"sort":"Start Date","order":"desc","limit":5}' \
  > "$OUT_DIR/test6.json"

# SAM.gov
if [ -z "$SAM_KEY" ]; then
  echo "SAM_GOV_API_KEY not set - skipping SAM tests"
else
  echo "Test 7: SAM entity search..."
  curl -s "https://api.sam.gov/entity-information/v3/entities?api_key=${SAM_KEY}&naicsCode=33&registrationDate=[2026-03-01,2026-03-13]&includeSections=entityRegistration,coreData&limit=5" > "$OUT_DIR/test7.json" || true

  UEI=$(jq -r '.entityData[0].ueiSAM // empty' "$OUT_DIR/test7.json" 2>/dev/null || echo "")
  echo "Test 8: SAM by UEI $UEI..."
  if [ -n "$UEI" ]; then
    curl -s "https://api.sam.gov/entity-information/v3/entities?api_key=${SAM_KEY}&ueiSAM=${UEI}&includeSections=entityRegistration,coreData" > "$OUT_DIR/test8.json" || true
  else
    echo '{"error":"No UEI from Test 7"}' > "$OUT_DIR/test8.json"
  fi

  echo "Test 9: SAM by name..."
  curl -s "https://api.sam.gov/entity-information/v3/entities?api_key=${SAM_KEY}&legalBusinessName=ACME&includeSections=entityRegistration,coreData&limit=5" > "$OUT_DIR/test9.json" || true

  echo "Test 10: SAM CSV..."
  curl -s "https://api.sam.gov/entity-information/v3/entities?api_key=${SAM_KEY}&naicsCode=33&registrationDate=[2026-03-01,2026-03-13]&format=csv&limit=5" > "$OUT_DIR/test10.csv" || true

  echo "Test 11: SAM full sections..."
  if [ -n "$UEI" ]; then
    curl -s "https://api.sam.gov/entity-information/v3/entities?api_key=${SAM_KEY}&ueiSAM=${UEI}&includeSections=entityRegistration,coreData,generalInformation,repsAndCerts,pointsOfContact" > "$OUT_DIR/test11.json" || true
  else
    echo '{"error":"No UEI from Test 7"}' > "$OUT_DIR/test11.json"
  fi

  echo "Test 12: SAM extracts monthly..."
  curl -s "https://api.sam.gov/data-services/v1/extracts?api_key=${SAM_KEY}&fileType=ENTITY&sensitivity=PUBLIC&frequency=MONTHLY" > "$OUT_DIR/test12.json" || true

  echo "Test 13: SAM extracts daily..."
  curl -s "https://api.sam.gov/data-services/v1/extracts?api_key=${SAM_KEY}&fileType=ENTITY&sensitivity=PUBLIC&frequency=DAILY&date=03/12/2026" > "$OUT_DIR/test13.json" || true

  echo "Test 14: SAM opportunities..."
  curl -s "https://api.sam.gov/opportunities/v2/search?api_key=${SAM_KEY}&postedFrom=03/01/2026&postedTo=03/13/2026&ptype=o&limit=5" > "$OUT_DIR/test14.json" || true
fi

echo "Done. Output in $OUT_DIR"
