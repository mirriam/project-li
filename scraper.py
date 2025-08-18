import requests
from bs4 import BeautifulSoup
import logging
import time
import re
from urllib.parse import urljoin, urlparse, parse_qs, unquote, quote
import base64
import json
import hashlib
import random
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import os
import subprocess

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# HTTP headers with randomized User-Agent
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15'
]
def get_headers():
    return {'user-agent': random.choice(USER_AGENTS)}

# Constants
WP_URL = "https://mauritius.mimusjobs.com/wp-json/wp/v2/job-listings"
WP_COMPANY_URL = "https://mauritius.mimusjobs.com/wp-json/wp/v2/company"
WP_MEDIA_URL = "https://mauritius.mimusjobs.com/wp-json/wp/v2/media"
WP_JOB_TYPE_URL = "https://mauritius.mimusjobs.com/wp-json/wp/v2/job_listing_type"
WP_JOB_REGION_URL = "https://mauritius.mimusjobs.com/wp-json/wp/v2/job_listing_region"
PROCESSED_IDS_FILE = "mauritius_processed_job_ids.csv"
LAST_PAGE_FILE = "last_processed_page.txt"
LIVE_JOBS_FILE = "scraped_jobs.json"
JOB_TYPE_MAPPING = {
    "Full-time": "full-time",
    "Part-time": "part-time",
    "Contract": "contract",
    "Temporary": "temporary",
    "Freelance": "freelance",
    "Internship": "internship",
    "Volunteer": "volunteer"
}
FRENCH_TO_ENGLISH_JOB_TYPE = {
    "Temps plein": "Full-time",
    "Temps partiel": "Part-time",
    "Contrat": "Contract",
    "Temporaire": "Temporary",
    "Indépendant": "Freelance",
    "Stage": "Internship",
    "Bénévolat": "Volunteer"
}

# Get environment variables with fallbacks
COUNTRY = os.environ.get('COUNTRY', 'Mauritius')
SPECIALTY = os.environ.get('SPECIALTY', 'software engineer')
WP_USERNAME = os.environ.get('WP_USERNAME')
WP_APP_PASSWORD = os.environ.get('WP_APP_PASSWORD')

# Proxy support (optional, uncomment to use)
# PROXIES = {
#     'http': 'http://your_proxy:port',
#     'https': 'http://your_proxy:port'
# }

def sanitize_text(text):
    if not text:
        return ''
    text = re.sub(r'\s+', ' ', text.strip())
    return text

def normalize_for_deduplication(text):
    if not text:
        return ''
    text = text.lower()
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text.strip())
    return text

def split_paragraphs(text, max_length=1000):
    if not text:
        return []
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    paragraphs = []
    current_paragraph = ""
    for sentence in sentences:
        if len(current_paragraph) + len(sentence) < max_length:
            current_paragraph += sentence + " "
        else:
            paragraphs.append(current_paragraph.strip())
            current_paragraph = sentence + " "
    if current_paragraph:
        paragraphs.append(current_paragraph.strip())
    return paragraphs

def get_or_create_term(term_name, endpoint, auth):
    term_name_normalized = normalize_for_deduplication(term_name)
    if not term_name_normalized:
        return None
    try:
        response = requests.get(endpoint, auth=auth, headers=get_headers(), timeout=10)
        if response.status_code == 200:
            for term in response.json():
                if normalize_for_deduplication(term['name']) == term_name_normalized:
                    return term['id']
        response = requests.post(endpoint, json={'name': term_name}, auth=auth, headers=get_headers(), timeout=10)
        if response.status_code in [200, 201]:
            return response.json()['id']
        logger.error(f"Failed to create term {term_name}: {response.status_code} {response.text}")
    except Exception as e:
        logger.error(f"Error creating term {term_name}: {str(e)}")
    return None

def save_company_to_wordpress(company_name, company_logo, auth):
    if not company_name:
        return None
    company_id = get_or_create_term(company_name, WP_COMPANY_URL, auth)
    if company_logo and company_id:
        try:
            logo_response = requests.get(company_logo, headers=get_headers(), timeout=10)
            if logo_response.status_code == 200:
                media_response = requests.post(
                    WP_MEDIA_URL,
                    headers={
                        'Content-Type': 'image/jpeg',
                        'Content-Disposition': f'attachment; filename={company_name}_logo.jpg'
                    },
                    data=logo_response.content,
                    auth=auth
                )
                if media_response.status_code in [200, 201]:
                    media_id = media_response.json()['id']
                    requests.post(
                        f"{WP_COMPANY_URL}/{company_id}",
                        json={'meta': {'company_logo': media_id}},
                        auth=auth,
                        headers=get_headers()
                    )
        except Exception as e:
            logger.error(f"Failed to upload company logo for {company_name}: {str(e)}")
    return company_id

def save_article_to_wordpress(job_data, live_jobs, auth):
    job_id = job_data.get('job_id')
    if not job_id:
        logger.warning("No job_id provided, skipping WordPress post")
        return None
    try:
        with open(PROCESSED_IDS_FILE, 'r') as f:
            processed_ids = f.read().splitlines()
    except FileNotFoundError:
        processed_ids = []
    if job_id in processed_ids:
        logger.info(f"Skipping already processed job: {job_id}")
        return None
    company_id = save_company_to_wordpress(job_data.get('company_name'), job_data.get('company_logo'), auth)
    job_type_id = get_or_create_term(job_data.get('job_type'), WP_JOB_TYPE_URL, auth)
    job_region_id = get_or_create_term(COUNTRY, WP_JOB_REGION_URL, auth)
    description_paragraphs = split_paragraphs(job_data.get('job_description', ''))
    article_data = {
        'title': job_data.get('job_title', ''),
        'content': '<p>' + '</p><p>'.join(description_paragraphs) + '</p>',
        'status': 'publish',
        'meta': {
            'job_id': job_id,
            'company_name': job_data.get('company_name', ''),
            'source': 'linkedin_scraper',
            'job_location': job_data.get('job_location', ''),
            'apply_url': job_data.get('apply_url', ''),
            'country': COUNTRY,
            'specialty': SPECIALTY
        }
    }
    if company_id:
        article_data['meta']['company'] = company_id
    if job_type_id:
        article_data['meta']['job_type'] = job_type_id
    if job_region_id:
        article_data['meta']['job_region'] = job_region_id
    try:
        response = requests.post(WP_URL, json=article_data, auth=auth, headers=get_headers(), timeout=10)
        if response.status_code in [200, 201]:
            with open(PROCESSED_IDS_FILE, 'a') as f:
                f.write(job_id + '\n')
            live_jobs.append({
                'title': job_data.get('job_title', ''),
                'company': job_data.get('company_name', ''),
                'country': COUNTRY,
                'specialty': SPECIALTY,
                'timestamp': time.strftime("%Y-%m-%d %H:%M:%S")
            })
            logger.info(f"Added job to live_jobs: {job_data.get('job_title')}, Total live jobs: {len(live_jobs)}")
            return response.json()['id']
        else:
            logger.error(f"Failed to post job {job_data.get('job_title')}: {response.status_code} {response.text}")
    except Exception as e:
        logger.error(f"Error posting job {job_data.get('job_title')}: {str(e)}")
    return None

def load_last_page():
    try:
        with open(LAST_PAGE_FILE, 'r') as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 1

def save_last_page(page):
    try:
        with open(LAST_PAGE_FILE, 'w') as f:
            f.write(str(page))
        logger.info(f"Saved last processed page: {page}")
    except Exception as e:
        logger.error(f"Failed to save last page: {str(e)}")

def save_live_jobs(live_jobs):
    try:
        live_jobs_data = live_jobs if live_jobs else []
        with open(LIVE_JOBS_FILE, 'w') as f:
            json.dump(live_jobs_data, f, indent=2)
        logger.info(f"Saved {len(live_jobs_data)} live jobs to {LIVE_JOBS_FILE}")
    except Exception as e:
        logger.error(f"Failed to save live jobs to {LIVE_JOBS_FILE}: {str(e)}")

def commit_files():
    try:
        subprocess.run(['git', 'config', '--local', 'user.email', 'github-actions@github.com'], check=True)
        subprocess.run(['git', 'config', '--local', 'user.name', 'GitHub Actions'], check=True)
        subprocess.run(['git', 'add', PROCESSED_IDS_FILE, LAST_PAGE_FILE, LIVE_JOBS_FILE], check=True)
        status = subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True).stdout
        logger.info(f"Git status: {status}")
        if status.strip():
            subprocess.run(['git', 'commit', '-m', 'Update scraped_jobs.json after scrape'], check=True)
            subprocess.run(['git', 'push'], check=True)
            logger.info("Successfully committed and pushed files to GitHub")
        else:
            logger.info("No changes to commit")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to commit files: {str(e)}")

def crawl(auth):
    live_jobs = []
    start_page = load_last_page()
    session = requests.Session()
    retries = Retry(total=5, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
    session.mount('https://', HTTPAdapter(max_retries=retries))
    
    # Try multiple search parameters
    search_params = [
        {'country': COUNTRY, 'specialty': SPECIALTY or 'software engineer'},
        {'country': 'United States', 'specialty': 'developer'},
        {'country': 'United Kingdom', 'specialty': 'software engineer'}
    ]
    
    for params in search_params:
        country = params['country']
        specialty = params['specialty']
        encoded_specialty = quote(specialty)
        encoded_location = quote(country)
        found_jobs = False
        
        logger.info(f"Trying search parameters: country={country}, specialty={specialty}")
        
        for page in range(start_page, start_page + 10):  # Increased to 10 pages
            start = (page - 1) * 25
            search_url = f"https://www.linkedin.com/jobs/search?keywords={encoded_specialty}&location={encoded_location}&start={start}"
            logger.info(f"Fetching job search page: {search_url}")
            
            try:
                response = session.get(search_url, headers=get_headers(), timeout=15) #, proxies=PROXIES)
                logger.info(f"Search page response: HTTP {response.status_code}")
                if response.status_code != 200:
                    logger.error(f"Failed to fetch search page: {response.status_code} {response.text[:200]}")
                    continue
                soup = BeautifulSoup(response.text, 'html.parser')
                
                # Check for CAPTCHA or login wall
                if 'checkpoint' in response.url or soup.select_one('input[id="username"]') or 'Sign in' in soup.get_text():
                    logger.error("Login or CAPTCHA detected, stopping crawl for this parameter set")
                    break
                
                # Updated selector for job cards
                job_cards = soup.select('li.jobs-search-results__list-item')
                job_urls = []
                for card in job_cards:
                    link = card.select_one('a.job-card-list__title')
                    if link and 'href' in link.attrs:
                        job_urls.append(urljoin('https://www.linkedin.com', link['href']))
                logger.info(f"Found {len(job_urls)} job URLs on page {page}")
                
                if not job_urls:
                    logger.info(f"No jobs found on page {page}, trying next page")
                    continue
                
                found_jobs = True
                for job_url in job_urls:
                    logger.info(f"Scraping job: {job_url}")
                    job_data = scrape_job_details(job_url, session)
                    if job_data:
                        job_id = job_data.get('job_id')
                        if job_id:
                            article_id = save_article_to_wordpress(job_data, live_jobs, auth)
                            if article_id:
                                logger.info(f"Successfully posted job {job_data.get('job_title')} to WordPress")
                            save_last_page(page)
                    time.sleep(random.uniform(15, 20))  # Increased delay
                
                if live_jobs:
                    logger.info(f"Jobs found, stopping parameter search")
                    break
            
            except Exception as e:
                logger.error(f"Error crawling page {page}: {str(e)}")
                time.sleep(random.uniform(5, 10))
                continue
        
        if found_jobs and live_jobs:
            logger.info(f"Found {len(live_jobs)} jobs with parameters: country={country}, specialty={specialty}")
            break
    
    save_live_jobs(live_jobs)
    if live_jobs:
        commit_files()
    else:
        logger.warning("No jobs scraped, not committing")
    return live_jobs

def scrape_job_details(job_url, session):
    try:
        response = session.get(job_url, headers=get_headers(), timeout=15) #, proxies=PROXIES)
        logger.info(f"Job page response: HTTP {response.status_code}")
        if response.status_code != 200:
            logger.error(f"Failed to fetch job page {job_url}: {response.status_code}")
            return None
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Updated selectors (based on LinkedIn structure, August 2025)
        job_title = soup.select_one('h1.jobs-unified-top-card__job-title')
        job_title = sanitize_text(job_title.get_text()) if job_title else ''
        logger.info(f"Scraped Job Title: {job_title}")
        
        company_name = soup.select_one('a.jobs-unified-top-card__company-name')
        company_name = sanitize_text(company_name.get_text()) if company_name else ''
        logger.info(f"Scraped Company Name: {company_name}")
        
        company_logo = soup.select_one('img.jobs-unified-top-card__company-logo')
        company_logo = company_logo['src'] if company_logo and 'src' in company_logo.attrs else ''
        logger.info(f"Scraped Company Logo: {company_logo}")
        
        job_location = soup.select_one('span.jobs-unified-top-card__bullet')
        job_location = sanitize_text(job_location.get_text()) if job_location else ''
        logger.info(f"Scraped Job Location: {job_location}")
        
        job_type = soup.select_one('li.jobs-unified-top-card__job-insight:nth-child(1) span')
        job_type = sanitize_text(job_type.get_text()) if job_type else ''
        job_type = FRENCH_TO_ENGLISH_JOB_TYPE.get(job_type, JOB_TYPE_MAPPING.get(job_type, job_type))
        logger.info(f"Scraped Job Type: {job_type}")
        
        job_description = soup.select_one('div.jobs-description-content__text')
        job_description = sanitize_text(job_description.get_text()) if job_description else ''
        logger.info(f"Scraped Job Description: {len(job_description)} characters")
        
        job_id_match = re.search(r'jobs/view/(\d+)', job_url)
        job_id = job_id_match.group(1) if job_id_match else ''
        logger.info(f"Scraped Job ID: {job_id}")
        
        apply_url = soup.select_one('a.jobs-apply-button')
        apply_url = apply_url['href'] if apply_url and 'href' in apply_url.attrs else job_url
        logger.info(f"Scraped Apply URL: {apply_url}")
        
        if not job_title or not job_id:
            logger.warning(f"Skipping job {job_url}: Missing title or ID")
            return None
        
        return {
            'job_id': job_id,
            'job_title': job_title,
            'company_name': company_name,
            'company_logo': company_logo,
            'job_location': job_location,
            'job_type': job_type,
            'job_description': job_description,
            'apply_url': apply_url
        }
    except Exception as e:
        logger.error(f"Error scraping job {job_url}: {str(e)}")
        return None

def main():
    if not WP_USERNAME or not WP_APP_PASSWORD:
        logger.error("WP_USERNAME or WP_APP_PASSWORD not set")
        return
    auth = (WP_USERNAME, WP_APP_PASSWORD)
    logger.info("Starting scraper with WP credentials")
    live_jobs = crawl(auth)
    logger.info(f"Scraping completed. Total jobs: {len(live_jobs)}")

if __name__ == "__main__":
    main()
