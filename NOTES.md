# NOTES: where the live API differed from our assumptions

Checked against docs.crustdata.com (API version `2025-11-01`) on 22 Nov 2026.

| Call | First guess (draft) | Docs say (now in `app/sources.py`) |
|---|---|---|
| Identify | `POST /company/identify` `{domains:[...]}` | Correct. Response `[{matched_on, match_type, matches:[{confidence_score, company_data}]}]`. `company_data` holds only `crustdata_company_id`, `basic_info`, `social_profiles`: **no headcount** (our fixture had it; removed). |
| Enrich | `POST /company/enrich` `{crustdata_company_ids:[id]}`, response = one object with `headcount.timeseries` | Same path, but the response is the **same array shape as identify**. Without `fields` only `basic_info` comes back, so we send `fields: [basic_info, headcount, locations, news]`. There is **no headcount timeseries**: we read `headcount.total` + `headcount.growth_percent.yoy` (a percentage). HQ is `locations.headquarters / state / country`. Enrich also returns dated `news[]` (`article_title`, `article_url`, `article_publish_date`). |
| Person search | `POST /person/search`, filters `current_employer_id`, `name`; response `profiles[].title` | Filters are `{op:"and", conditions:[{field, type, value}]}` with fields `experience.employment_details.current.company_id` and `basic_profile.name` (fuzzy `(.)`). Title is `basic_profile.current_title` (requested via `fields`). |
| Web search | `POST /web/search` `{query, limit}`, results with `date` | Path is **`POST /web/search/live`**, body `{query, location:"IN", sources:["news"]}`. Results are `{source, title, url, snippet, position}`: **no date field**. Dated news comes from enrich `news[]` instead. |
| Credits | hard-coded `1.0` per call | Every data response carries an **`X-Credits-Used`** header; live calls record that. Mock estimates use the documented prices (identify 0.2/match, web search 1/page, person search 0.03/result + 0.1 per premium group filtered/received). |
| Errors | 401/403 retried like any error | 401/403 fail fast (`AuthError`), 402 never retried, 429 waits for `x-ratelimit-reset`. |

Rate limits from the docs: enrich 15/min, person search 30/min, web search 10/min.
