target_sub: SEO
Does anyone actually fix "thin content" warnings, or do you just ignore them?
Every audit tool flags pages under 300 words. But category pages, contact pages, legal stuff, they're supposed to be short. Curious where you personally draw the line between "needs more content" and "it's fine, move on".
---
target_sub: SEO
How often do you re-audit client sites after the initial cleanup?
Fixed everything six months ago, came back and found 40 new broken links because the client kept editing pages. Monthly feels like overkill for small sites, yearly feels negligent. What cadence works for you?
---
target_sub: SEO
Duplicate title tags across paginated pages, worth fixing or not?
Client's blog has /page/2, /page/3 etc all with the same title. Google seems to handle pagination fine these days. Has anyone seen actual ranking impact from cleaning this up, or is it just audit-tool noise?
---
target_sub: SEO
What's the first thing you check when a site's traffic drops and there's no obvious penalty?
I usually start with Search Console coverage report, then redirects, then robots.txt history. But I keep second-guessing my order of operations. What's your triage sequence?
---
target_sub: SEO
Do meta descriptions still matter enough to write them manually?
Google rewrites most of them anyway. I still write them for money pages but let the rest auto-generate. Am I being lazy or pragmatic?
---
target_sub: SEO
Clients who won't fix HTTP to HTTPS redirects because "the site works fine", how do you convince them?
I've shown mixed content warnings, browser "not secure" labels, the works. Some just don't care until rankings dip. What argument finally landed for you?
---
target_sub: SEO
Is anyone still using XML sitemaps for small sites, like under 100 pages?
Google crawls small sites fine without one. But every audit checklist says "add a sitemap". Genuine question: has a sitemap ever made a measurable difference on a small, well-linked site in your experience?
---
target_sub: TechSEO
Redirect chains: at what length do you actually start caring?
Two hops seems harmless, but I've inherited sites with 5+ hop chains from years of migrations. Where's the practical threshold where you've seen crawl or equity impact, not just theoretical?
---
target_sub: TechSEO
How do you audit JS-heavy sites where half the content isn't in the raw HTML?
Client site is React with partial SSR. Crawlers that don't render JS report missing H1s and empty pages, which is technically wrong but also kind of right. How do you separate real issues from rendering artifacts?
---
target_sub: TechSEO
Canonical pointing to a redirecting URL, how bad is this in practice?
Found a pattern where canonicals point to URLs that 301 elsewhere. Google docs say don't do it, but the site ranks fine. Has anyone measured actual impact from fixing canonical-to-redirect chains?
---
target_sub: TechSEO
Server response time vs Core Web Vitals: which moved the needle more for you?
Everyone obsesses over LCP and CLS, but I've seen bigger indexing improvements from cutting TTFB on slow shared hosting. Curious what others have observed on medium-size sites.
---
target_sub: TechSEO
Do security headers (HSTS, X-Content-Type-Options) belong in an SEO audit?
I include them because they're cheap to check and clients like the thoroughness. A colleague says it's scope creep and nothing to do with rankings. Where do you stand?
---
target_sub: TechSEO
What's your process for finding orphan pages without server log access?
Sitemap-vs-crawl diff catches some, Search Console catches others. But on client sites where I can't get logs, I always feel like I'm missing pages. Any techniques I'm overlooking?
---
Do you run any SEO checks before shipping, or is that "someone else's job"?
Genuinely curious how dev teams handle this. Broken internal links, missing meta tags, redirect chains, this stuff usually ships and gets found months later by whoever runs marketing. Is SEO QA part of anyone's CI or pre-launch checklist?
---
target_sub: TechSEO
Render-blocking scripts in head: do you actually defer everything, or is it cargo cult at this point?
Every perf tool screams about it, but with HTTP/2 and modern caching the real-world difference on small sites seems tiny. When did deferring scripts last give you a measurable win?
---
target_sub: TechSEO
How do you handle trailing slash consistency, server config or framework level?
Half the internal links on a project I inherited go to /page and half to /page/, each causing a 301. Fixing at nginx level vs fixing the links themselves, what's your preference and why?
---
What's the most annoying thing a CMS did to your site's URLs?
WordPress once regenerated an entire site's permalinks after a plugin update and nobody noticed for a month. Thousands of 404s. What's your horror story, and how do you catch this stuff early now?
---
target_sub: SEO_tools_reviews
What free tools do you actually keep using after trying the paid suites?
Everyone demos Ahrefs and Semrush, but I'm curious what survives in your workflow long-term without a subscription. Browser extensions, CLI tools, spreadsheet templates, anything that stuck.
---
target_sub: SEO_tools_reviews
Screaming Frog's 500 URL free limit: enough for small sites or constantly hitting the wall?
For quick checks on small business sites it mostly works, but the moment there's a blog archive it's over. How do you handle audits in the 500-2000 page range without paying for a full license?
---
target_sub: SEO_tools_reviews
Do all-in-one audit scores (like "SEO score 87/100") mean anything to you?
Clients love a single number, but two tools give the same site wildly different scores. Do you show these scores to clients or hide them and report specific issues instead?
---
target_sub: digital_marketing
Small business owners: what made you finally take technical SEO seriously?
Most owners I talk to care about content and ads, and technical stuff feels invisible until something breaks. If you run a small site, what was the moment you realized broken links or slow pages were costing you?
---
target_sub: digital_marketing
Agencies: do you include a technical audit in every new client onboarding?
Some agencies audit everything upfront, others only when there's a visible problem. Upfront audits catch issues early but add cost to onboarding. What's your model and has it changed over time?
---
target_sub: digital_marketing
How do you explain technical SEO issues to a client who only understands leads and revenue?
"You have 200 broken links" means nothing to them. I've started translating everything into "pages that can't sell" language. What framings have worked for you?
---
target_sub: SideProject
Those of you who built browser extensions: was Chrome Web Store review as painful for you as everyone says?
Reviews taking weeks, rejections with vague reasons, policy changes mid-review. Curious about recent experiences, is it getting better or worse in 2026?
---
target_sub: SideProject
How do you market a side project when the target audience isn't on tech Twitter?
My users are freelancers and small agency folks, not developers. Product Hunt and HN don't reach them. Where did you find your first hundred non-developer users?
---
target_sub: chrome_extensions
MV3 service worker dying mid-task: what's your least ugly workaround?
Mine does long-running fetch work and the 30s idle kill keeps biting me. I chunk the work and persist state to chrome.storage between wakeups, which works but feels like fighting the platform. Offscreen documents? Alarms? What's held up best for you in production?
---
target_sub: chrome_extensions
How long did your last Chrome Web Store review take, and what got you rejected?
My last update sat in review for a while and the rejection reasons were vague enough that I fixed things by guessing. Curious what the current turnaround looks like for others and whether there's a pattern to what triggers manual review.
