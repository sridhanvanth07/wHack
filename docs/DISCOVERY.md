# Internship Discovery Engine

## Source coverage

CarryFlow queries bounded, read-only listing sources and sends users to the source listing to apply. It never submits applications.

- **Remotive** is enabled by default and needs no API key. Its public API is remote-work focused, so it will not represent the full India internship market. The service recommends no more than four requests per day, delays listings by 24 hours, and requires Remotive attribution and a link back to each source listing. CarryFlow caches successful responses for eight hours and observes a minimum interval between calls. See the [Remotive API documentation](https://github.com/remotive-com/remote-jobs-api/blob/main/README.md).
- **Adzuna India** is optional. Set `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` in the server environment to enable it. It searches India listings using keywords and location; results are redirected to the employer/source listing. Adzuna documents the credentials in its [API overview](https://developer.adzuna.com/overview) and [search API](https://developer.adzuna.com/docs/search). Its terms require visible Adzuna branding linked to Adzuna, and commercial use beyond the evaluation period needs a separate licence. CarryFlow displays its attribution on search results. Review the [Adzuna API terms](https://developer.adzuna.com/docs/terms_of_service) before deployment.

No listing site is scraped. Missing data remains unknown; source coverage and failures are shown beside results. Search defaults to ten results and is capped at twenty. Results are filtered and ranked locally, deduplicated, and retain source URLs and attribution.

## Search behavior

The local intent parser supports role/skill keywords, a maintained set of common India cities and country, remote/hybrid/onsite, paid/unpaid wording, result counts, student year, company hints, recent-posted windows, a minimum compensation when currency is explicit, and student eligibility wording. A session retains its latest validated search for two hours; criteria-only followups refine that request, and “show earlier results” reads only that chat's saved search context. Recent listings and search criteria are isolated by chat session. This is deterministic parsing; it does not call an LLM or infer eligibility that the listing does not state.

Results are classified as verified, potential, or insufficient-information matches. Known contradictory role or hard-filter evidence can exclude a result, while missing compensation, eligibility, work mode, or date information is surfaced with a caveat instead of silently treated as a match. Search pages preserve source attribution and the source's original link.

## Local setup

Remotive works without configuration. To enable Adzuna, set both credentials in the backend process environment or a local `.env` file (never in frontend code). Search requests may omit either source when unavailable; the response reports each source status and continues with remaining sources.
