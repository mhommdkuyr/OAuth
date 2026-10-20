#!/usr/bin/env python3
"""Resumable, conservative public-website enrichment for Ibb listings.
Collects published contact/social links and image URLs; does not download/rehost images or bypass access controls.
"""
from __future__ import annotations
import argparse, csv, html, ipaddress, json, os, re, socket, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.robotparser import RobotFileParser

ROOT=Path(__file__).resolve().parent
DB_JSON=ROOT/"ibb_city_complete_database.json"
DB_CSV=ROOT/"ibb_city_complete_database.csv"
REPORT=ROOT/"scrape_report.json"
CACHE_PATH=ROOT/"website_enrichment_cache.json"
UA="IbbCensusBot/1.0 (+https://github.com/mhommdkuyr/OAuth)"
EMAIL_RE=re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SOCIAL={
 "instagram":re.compile(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)",re.I),
 "facebook":re.compile(r"https?://(?:www\.|m\.|web\.)?facebook\.com/([A-Za-z0-9_.\-]+)",re.I),
 "linkedin":re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:company|in|school)/([A-Za-z0-9_.%\-]+)",re.I),
}
SKIP={"home","about","help","privacy","policies","login","share","sharer","p","reel","reels","stories","explore","watch","groups","events","marketplace","plugins","dialog","intent","search","profile.php"}
EMPTY={"","none","null","nan","n/a","na","[]","{}"}

def now(): return datetime.now(timezone.utc).isoformat()
def good(v):
 if v is None or v is False: return False
 if isinstance(v,str):
  s=v.strip()
  if not s or s.casefold() in EMPTY: return False
  if s[:1] in "[{" and s[-1:] in "]}":
   try: return good(json.loads(s))
   except Exception: return True
  return True
 if isinstance(v,dict): return any(good(x) for x in v.values())
 if isinstance(v,(list,tuple,set)): return any(good(x) for x in v)
 return True
def atomic(path,text):
 tmp=path.with_suffix(path.suffix+".tmp"); tmp.write_text(text,encoding="utf-8"); os.replace(tmp,path)
def norm_url(url):
 try:
  p=urllib.parse.urlsplit(str(url or "").strip())
  if p.scheme not in ("http","https") or not p.hostname: return ""
  return urllib.parse.urlunsplit((p.scheme.lower(),p.netloc.lower(),p.path or "/", "", ""))
 except Exception: return ""
def host(url):
 p=urllib.parse.urlsplit(norm_url(url)); return (p.hostname or "").lower().removeprefix("www.")
def public_url(url):
 p=urllib.parse.urlsplit(norm_url(url)); h=p.hostname or ""
 if not h or h in ("localhost","localhost.localdomain") or h.endswith((".local",".internal",".localhost")): return False
 try: return ipaddress.ip_address(h).is_global
 except ValueError: pass
 try:
  ips={v[4][0] for v in socket.getaddrinfo(h,p.port or (443 if p.scheme=="https" else 80))}
  return bool(ips) and all(ipaddress.ip_address(x).is_global for x in ips)
 except Exception: return False

class Parser(HTMLParser):
 def __init__(self):
  super().__init__(convert_charrefs=True); self.meta={}; self.images=[]; self.links=[]; self.scripts=[]
  self._json=False; self._buf=[]
 def handle_starttag(self,tag,attrs):
  a={k.lower():(v or "") for k,v in attrs}
  if tag=="meta":
   k=(a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
   if k and a.get("content"): self.meta.setdefault(k,a["content"].strip())
  elif tag in ("img","source"):
   for raw in (a.get("src") or a.get("data-src") or a.get("data-lazy-src") or "", a.get("srcset","").split(",")[0].strip().split(" ")[0]):
    if raw: self.images.append((raw,a.get("width",""),a.get("height","")))
  elif tag=="a" and a.get("href"):
   self.links.append((a["href"],a.get("aria-label","")))
  elif tag=="script" and "ld+json" in a.get("type","").lower(): self._json=True; self._buf=[]
 def handle_endtag(self,tag):
  if tag=="script" and self._json: self.scripts.append("".join(self._buf)); self._json=False; self._buf=[]
 def handle_data(self,data):
  if self._json: self._buf.append(data)

def jsonld_objects(scripts):
 out=[]
 def walk(v):
  if isinstance(v,dict):
   out.append(v)
   for k,x in v.items():
    if k!="@graph" and isinstance(x,(dict,list)): walk(x)
  elif isinstance(v,list):
   for x in v: walk(x)
 for raw in scripts:
  try: walk(json.loads(raw))
  except Exception: pass
 return sorted(out,key=lambda x:sum(bool(x.get(k)) for k in ("name","address","telephone","email","openingHours","image","geo")),reverse=True)
def flatten_images(v):
 if isinstance(v,str): return [v]
 if isinstance(v,list): return [y for x in v for y in flatten_images(x)]
 if isinstance(v,dict): return flatten_images(v.get("url") or v.get("contentUrl") or "")
 return []
def addrtext(v):
 if isinstance(v,str): return v.strip()
 if isinstance(v,dict):
  return ", ".join(str(v[k]) for k in ("streetAddress","addressLocality","addressRegion","postalCode","addressCountry") if v.get(k))
 return ""
def fetch_robots(url):
 p=urllib.parse.urlsplit(url); origin=f"{p.scheme}://{p.netloc}"
 req=urllib.request.Request(origin+"/robots.txt",headers={"User-Agent":UA})
 try:
  with urllib.request.urlopen(req,timeout=8) as resp: lines=resp.read(100000).decode("utf-8","replace").splitlines()
  rp=RobotFileParser(origin+"/robots.txt"); rp.parse(lines)
  return rp, min(max(float(rp.crawl_delay(UA) or rp.crawl_delay("*") or 1),1),8),"robots_ok"
 except urllib.error.HTTPError as e:
  rp=RobotFileParser(origin+"/robots.txt")
  if e.code==404: rp.parse([]); return rp,1,"robots_not_found"
  rp.parse(["User-agent: *","Disallow: /"]); return rp,1,f"robots_http_{e.code}"
 except Exception:
  rp=RobotFileParser(origin+"/robots.txt"); rp.parse(["User-agent: *","Disallow: /"])
  return rp,1,"robots_unavailable_skipped"
def fetch_page(url,rp):
 url=norm_url(url)
 if not url or not public_url(url): return None,"unsafe_or_invalid_url"
 if not rp.can_fetch(UA,url): return None,"robots_disallowed"
 req=urllib.request.Request(url,headers={"User-Agent":UA,"Accept":"text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"})
 try:
  with urllib.request.urlopen(req,timeout=12) as resp:
   final=norm_url(resp.geturl()); ctype=resp.headers.get("Content-Type","").lower()
   if not final or not public_url(final): return None,"redirect_to_nonpublic_url"
   if ctype and "html" not in ctype and "xhtml" not in ctype: return None,"not_html"
   return (final,resp.read(500000).decode(resp.headers.get_content_charset() or "utf-8","replace")),"ok"
 except urllib.error.HTTPError as e: return None,f"http_{e.code}"
 except Exception as e: return None,"fetch_error:"+type(e).__name__
def extract(url,body,parser):
 objs=jsonld_objects(parser.scripts); best=objs[0] if objs else {}
 images=[]
 candidates=[parser.meta.get(k,"") for k in ("og:image","og:image:url","twitter:image","twitter:image:src","image")]
 for o in objs:
  candidates.extend(flatten_images(o.get("image")))
 for raw,w,h in parser.images:
  try:
   if (w and int(w)<80) or (h and int(h)<80): continue
  except Exception: pass
  candidates.append(raw)
 for raw in candidates:
  u=urllib.parse.urljoin(url,html.unescape(raw.strip()))
  if u.startswith(("http://","https://")) and not any(x in u.lower() for x in ("favicon","tracking","pixel","analytics","doubleclick","1x1")) and u not in images:
   images.append(u)
  if len(images)>=12: break
 text=html.unescape(re.sub(r"<[^>]+>"," ",body))
 emails=list(dict.fromkeys([e.lower().strip(".,;:()[]<>") for e in re.findall(EMAIL_RE,text)]+[e.lower().strip() for e in EMAIL_RE.findall(" ".join(o.get("email","") if isinstance(o.get("email"),str) else "" for o in objs))]))
 emails=[e for e in emails if len(e)<255 and not e.endswith((".png",".jpg",".jpeg",".webp",".svg"))][:8]
 phones=[]
 for href,_ in parser.links:
  if href.lower().startswith("tel:"): phones.append(urllib.parse.unquote(href[4:].split("?")[0]))
 for o in objs:
  if isinstance(o.get("telephone"),str): phones.append(o["telephone"])
 socials={"instagram":"","facebook":"","linkedin":""}
 for href,label in parser.links:
  u=urllib.parse.urljoin(url,html.unescape(href))
  for k,rx in SOCIAL.items():
   m=rx.search(u)
   if m and m.group(1).casefold() not in SKIP and not socials[k]: socials[k]=u
 for o in objs:
  vals=o.get("sameAs",[])
  if isinstance(vals,str): vals=[vals]
  if isinstance(vals,list):
   for u in vals:
    if not isinstance(u,str): continue
    for k,rx in SOCIAL.items():
     m=rx.search(u)
     if m and m.group(1).casefold() not in SKIP and not socials[k]: socials[k]=u
 geo=best.get("geo") if isinstance(best.get("geo"),dict) else {}
 hours=best.get("openingHours") or best.get("openingHoursSpecification") or ""
 if isinstance(hours,(dict,list)): hours=json.dumps(hours,ensure_ascii=False)
 return {
  "website_name":best.get("name","") if isinstance(best.get("name",""),str) else "",
  "website_emails":emails,
  "website_phones":list(dict.fromkeys(str(p).strip() for p in phones if p))[:5],
  "website_address":addrtext(best.get("address")),
  "website_opening_hours":hours,
  "website_latitude":str(geo.get("latitude","") or ""),
  "website_longitude":str(geo.get("longitude","") or ""),
  "website_images":images,
  **socials
 }
def enrich(url):
 url=norm_url(url); h=host(url)
 if not h or not public_url(url): return {"status":"skipped_unsafe_url","attempts":1,"checked_at_utc":now()}
 rp,delay,rs=fetch_robots(url)
 if not rp.can_fetch(UA,url): return {"status":"robots_disallowed","robots_status":rs,"attempts":1,"checked_at_utc":now()}
 page,status=fetch_page(url,rp)
 if not page: return {"status":status,"robots_status":rs,"attempts":1,"checked_at_utc":now()}
 final,body=page; p=Parser()
 try: p.feed(body)
 except Exception: pass
 data=extract(final,body,p)
 if not data["website_emails"] or not data["website_address"]:
  for href,label in p.links:
   if not re.search(r"contact|about|location|visit",href+" "+label,re.I): continue
   candidate=urllib.parse.urljoin(final,href)
   if host(candidate)!=h: continue
   time.sleep(delay); page2,_=fetch_page(candidate,rp)
   if not page2: continue
   url2,body2=page2; p2=Parser()
   try: p2.feed(body2)
   except Exception: pass
   d2=extract(url2,body2,p2)
   for k in ("website_emails","website_phones","website_images"):
    data[k]=list(dict.fromkeys(data[k]+d2[k]))[:12]
   for k in ("website_address","website_opening_hours","website_latitude","website_longitude","instagram","facebook","linkedin"):
    if not data.get(k): data[k]=d2.get(k,"")
   break
 return {"status":"ok","robots_status":rs,"final_url":final,"checked_at_utc":now(),"attempts":1,"data":data}

def main():
 ap=argparse.ArgumentParser()
 ap.add_argument("--batch-size",type=int,default=int(os.getenv("WEBSITE_BATCH_SIZE","25")))
 args=ap.parse_args()
 if not 1<=args.batch_size<=60: ap.error("--batch-size must be between 1 and 60")
 records=json.loads(DB_JSON.read_text(encoding="utf-8")) if DB_JSON.exists() else []
 if not isinstance(records,list): raise RuntimeError("Database JSON should be a list")
 cache=json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.exists() else {}
 if not isinstance(cache,dict): raise RuntimeError("Website cache is not a JSON object")
 candidates={}
 for row in records:
  u=norm_url(row.get("website","")); h=host(u)
  if not u or not h: continue
  ent=cache.get(h,{})
  status=ent.get("status",""); attempts=int(ent.get("attempts",0))
  if status in ("ok","robots_disallowed","skipped_unsafe_url","robots_unavailable_skipped"): continue
  if attempts<3: candidates.setdefault(h,u)
 chosen=list(candidates.items())[:args.batch_size]
 print(f"Website enrichment: processing {len(chosen)} domains; {len(candidates)} pending",flush=True)
 for h,u in chosen:
  old=int(cache.get(h,{}).get("attempts",0))
  try:
   ent=enrich(u); ent["attempts"]=old+1; cache[h]=ent
  except Exception as e:
   cache[h]={"status":"error:"+type(e).__name__,"attempts":old+1,"checked_at_utc":now()}
  atomic(CACHE_PATH,json.dumps(cache,ensure_ascii=False,indent=2)+"\n")
  time.sleep(1)
 for row in records:
  h=host(row.get("website","")); ent=cache.get(h,{})
  d=ent.get("data",{}) if ent.get("status")=="ok" else {}
  row["website_enrichment_status"]=ent.get("status","pending") if h else "no_website"
  row["website_enrichment_checked_at"]=ent.get("checked_at_utc","")
  for k in ("website_images","website_phones"):
   row[k]=d.get(k,[])
  for k in ("website_name","website_address","website_opening_hours","website_latitude","website_longitude"):
   row[k]=d.get(k,"")
  for k in ("instagram","facebook","linkedin"):
   if not good(row.get(k)) and good(d.get(k)): row[k]=d[k]
  if not good(row.get("email")) and good(d.get("website_emails")): row["email"]=d["website_emails"]
  photo_urls=[]; sources=[]
  t=row.get("thumbnail")
  if good(t) and str(t).startswith(("https://","http://")): photo_urls.append(t); sources.append("maps_thumbnail")
  raw=row.get("images",[])
  if isinstance(raw,str):
   try: raw=json.loads(raw)
   except Exception: raw=[]
  if isinstance(raw,list):
   for u in raw:
    if isinstance(u,str) and u.startswith(("https://","http://")) and u not in photo_urls: photo_urls.append(u); sources.append("listing_images")
  im=row.get("image","")
  if isinstance(im,str) and im.startswith(("https://","http://")) and im not in photo_urls: photo_urls.append(im); sources.append("openstreetmap")
  for u in row["website_images"]:
   if isinstance(u,str) and u.startswith(("https://","http://")) and u not in photo_urls: photo_urls.append(u); sources.append("business_website")
  row["photo_urls"]=photo_urls[:20]; row["photo_sources"]=list(dict.fromkeys(sources))
 atomic(DB_JSON,json.dumps(records,ensure_ascii=False,indent=2)+"\n")
 columns=list(dict.fromkeys(k for r in records for k in r.keys()))
 tmp=DB_CSV.with_suffix(".csv.tmp")
 with tmp.open("w",encoding="utf-8-sig",newline="") as f:
  w=csv.DictWriter(f,fieldnames=columns,extrasaction="ignore"); w.writeheader()
  for r in records: w.writerow({k:json.dumps(r.get(k),ensure_ascii=False,separators=(",",":")) if isinstance(r.get(k),(list,dict)) else ("" if r.get(k) is None else str(r.get(k))) for k in columns})
 os.replace(tmp,DB_CSV)
 report=json.loads(REPORT.read_text(encoding="utf-8")) if REPORT.exists() else {}
 def present(v):
  if v is None or v is False: return False
  if isinstance(v,str):
   s=v.strip()
   if not s or s.casefold() in EMPTY: return False
   if s[:1] in "[{" and s[-1:] in "]}":
    try: return present(json.loads(s))
    except Exception: return True
   return True
  if isinstance(v,dict): return any(present(x) for x in v.values())
  if isinstance(v,(list,tuple,set)): return any(present(x) for x in v)
  return True
 total=len(records); fields=["name","phone","email","website","category","address","rating","review_count","google_maps_url","latitude","longitude","place_id","opening_hours","thumbnail","images","photo_urls","website_images","website_phones","website_address","website_opening_hours","instagram","facebook","linkedin","street_view_url","owner","menu","order_online","reservations","price_range","about"]
 cov={}
 for field in fields:
  n=sum(1 for r in records if present(r.get(field)))
  cov[field]={"filled":n,"total":total,"percent":round(100*n/total,2) if total else 0}
 report["field_coverage"]=cov; report["output_column_count"]=len(columns)
 report["records_with_any_photo_url"]=sum(1 for r in records if present(r.get("photo_urls")))
 report["records_with_all_eight_core_fields"]=sum(1 for r in records if all(present(r.get(k)) for k in ("name","phone","email","website","category","address","rating","review_count")))
 report["website_enrichment"]={"cached_hosts":len(cache),"successful_hosts":sum(1 for x in cache.values() if x.get("status")=="ok"),"robots_disallowed_hosts":sum(1 for x in cache.values() if x.get("status")=="robots_disallowed"),"last_batch_hosts":len(chosen),"note":"Only public, robots-allowed pages are checked. Photo URLs are not downloaded/rehosted and are not guaranteed to show the address frontage."}
 atomic(REPORT,json.dumps(report,ensure_ascii=False,indent=2)+"\n")
 print(json.dumps({"database_rows":total,"cached_hosts":len(cache),"successful_hosts":report["website_enrichment"]["successful_hosts"],"records_with_any_photo_url":report["records_with_any_photo_url"],"columns":len(columns),"field_coverage":{k:v for k,v in cov.items() if k in ("phone","email","website","photo_urls","website_images","instagram","facebook","linkedin","street_view_url")}},ensure_ascii=False,indent=2),flush=True)
if __name__=="__main__": main()
