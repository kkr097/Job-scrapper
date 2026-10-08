"""
AI Job Rater - Rates jobs 1-10 based on resume match.
Supports: DeepSeek (free), Gemini, Groq, or local Ollama.
"""

import json
import time
from typing import List, Dict

import requests

from filter_policy import Policy
from title_rules import TITLE_PENALTY_MAX_SCORE

SPARSE_DESCRIPTION_CHARS = 80       # below this the model has no evidence to judge
SPARSE_DESCRIPTION_MAX_SCORE = 5      # so it can never reach the match threshold (6)


class JobRater:
    """Rate jobs against resume using AI (DeepSeek, Gemini, or Groq)."""
    
    def __init__(self, config: dict, candidate_profile: dict):
        self.candidate_profile = candidate_profile
        self.config = config
        rules = candidate_profile.get("matching_rules", {}) if isinstance(candidate_profile, dict) else {}
        self.policy = Policy.from_matching_rules(rules)
        # A job without evidence must stay below the match threshold, whatever min_score is.
        try:
            threshold = int(config.get("min_score", 6))
        except (TypeError, ValueError):
            threshold = 6
        self.sparse_cap = max(1, min(SPARSE_DESCRIPTION_MAX_SCORE, threshold - 1))
        
        # Determine which API to use
        self.api_type = None
        self.api_key = None

        self.has_groq = bool(config.get('groq_api_key', '').startswith('gsk_'))
        self.has_gemini = bool(config.get('gemini_api_key', '').startswith('AIza'))
        self.has_deepseek = bool(config.get('deepseek_api_key', '').startswith('sk-'))

        if self.has_groq and self.has_gemini:
            self.api_type = 'multi'
            print("   ? Using Groq + Gemini with smart switching")
        elif self.has_groq:
            self.api_type = 'groq'
            self.api_key = config['groq_api_key']
            print("   ? Using Groq (Llama) - FREE tier")
        elif self.has_gemini:
            self.api_type = 'gemini'
            self.api_key = config['gemini_api_key']
            print("   ? Using Gemini AI")
        elif self.has_deepseek:
            self.api_type = 'deepseek'
            self.api_key = config['deepseek_api_key']
            print("   ? Using DeepSeek AI")
        else:
            print("   ? No API key configured")
            self.api_type = None

        # LLM routing state for optimal switching
        self._llm_state = {
            "preferred": config.get("rating_preferred", "groq"),
            "groq_cooldown_until": 0,
            "gemini_cooldown_until": 0
        }

    def rate_jobs(self, jobs: List[Dict], batch_size: int = 5) -> List[Dict]:
        """Rate all jobs against the resume."""
        if not self.api_type:
            print("   No API available - assigning default scores")
            for job in jobs:
                job['score'] = 5
                job['match_reasons'] = 'Add API key to config.json'
                job['missing_skills'] = ''
            return jobs
        
        rated = []
        
        for i in range(0, len(jobs), batch_size):
            batch = jobs[i:i + batch_size]
            print(f"   Rating jobs {i+1}-{min(i+batch_size, len(jobs))}...")
            
            try:
                rated_batch = self._rate_batch(batch)
                rated.extend(rated_batch)
                
                  # Rate limiting is handled by the caller to allow async CSV updates.
                    
            except Exception as e:
                print(f"   ⚠ Rating error: {e}")
                for job in batch:
                    job['score'] = 5
                    job['match_reasons'] = f'Rating error: {str(e)[:30]}'
                    job['missing_skills'] = ''
                    rated.append(job)
        
        return rated
    
    def _rate_batch(self, jobs: List[Dict]) -> List[Dict]:
        """Rate a batch of jobs with AI."""
        prompt_profile = json.loads(json.dumps(self.candidate_profile))
        if isinstance(prompt_profile, dict):
            # Keyword rules are evaluated in code (filter_policy.py); hide the raw lists and the
            # title cap rule so the model cannot guess hits, e.g. "IT" in "it is".
            prompt_profile.get("matching_rules", {}).pop("negative_title_keywords", None)
            prompt_profile.get("matching_rules", {}).pop("negative_description_keywords", None)
            prompt_profile.get("scoring_protocol", {}).pop("title_filter_mandatory", None)
        candidate_json = json.dumps(prompt_profile, ensure_ascii=False)
        decisions = [self.policy.evaluate(j) for j in jobs]

        job_blocks = []
        for idx, job in enumerate(jobs, 1):
            decision = decisions[idx - 1]
            desc = (job.get("description") or "").strip()
            if len(desc) > 2000:
                desc = desc[:2000] + "..."
            block = "\n".join([
                f"JOB {idx}",
                f"Title: {job.get('title','')}",
                "Title penalty (checked by code, authoritative): " + (
                    f"HIT '{decision.rule}' - cap the score at {TITLE_PENALTY_MAX_SCORE}" if decision.cap else "none"
                ),
                "Policy signals (from code; weigh in context, not automatic rejection): " + (
                    "; ".join(decision.signals) or "none"
                ),
                f"Company: {job.get('company','')}",
                f"Location: {job.get('location','')}",
                f"URL: {job.get('url','')}",
                f"Description: {desc}"
            ])
            job_blocks.append(block)
        jobs_text = "\n\n".join(job_blocks)

        prompt = f"""### ROLE
Expert Technical Talent Matcher (Automotive & Embedded Systems).

### SYSTEM INSTRUCTIONS
Evaluate the [CANDIDATE_PROFILE] against the [JOB_DESCRIPTION] provided.
Follow the [SCORING_PROTOCOL] strictly.
Perform a "Step-by-Step Gap Analysis" before assigning the final score to ensure accuracy.

---

### CANDIDATE_PROFILE (JSON Data)
{candidate_json}

---

### JOB_DESCRIPTION (Extracted Text)
{jobs_text}

---

### SCORING_PROTOCOL (Ruleset)
1. **Technical Core (Match Weight: 50%):**
   - Must prioritize: C, C++, MATLAB/Simulink, TargetLink, ASPICE, V-Model.
   - Secondary: Python, CI/CD, Git, Unit Testing (cmocka).

2. **Domain Alignment (Match Weight: 30%):**
   - Positive Domains: Automotive, Medical Engineering, Robotics, Control Systems.
   - Positive Standards: ISO 26262, AUTOSAR, MISRA.

3. **Seniority & Role Fit (Penalty Weight: 20%):**
   - Candidate has ~4 years professional/academic experience (Mid-level).
   - APPLY HARD PENALTY (Score < 3) if the job's actual duties are:
     - Pure Management: people/budget/project management with little hands-on engineering
       (a title word such as Lead or Manager is only a signal; judge the duties).
     - Wrong Tech Stack: (Cloud, DevOps, Java, Web, Fullstack, SAP, PLC/SPS).
     - Entry Level: (Intern, Praktikant, Masterarbeit, Student).

---

Title penalties are decided by code: use ONLY the "Title penalty (checked by code,
authoritative)" line of each job. If it says "none", do not apply or mention any title
penalty; never infer one from words in the description or from the title's meaning.
The "Policy signals" line lists keyword matches found by code. They are evidence to
weigh in context (a signal can be wrong, e.g. a company name); they never mean automatic
rejection. If it says "none", do not claim any keyword penalty.

Score using this explicit calibration:
- 9-10: Strong match for the required stack and domain, with only minor gaps.
- 7-8: Good match with the main stack present and manageable missing skills.
- 5-6: Partial match; some relevant skills but important requirements are missing.
- 3-4: Weak match with limited technical overlap.
- 1-2: Hard title penalty or clearly incompatible role/domain.
Do not award a high score from a generic software title alone. Do not reduce a
score merely because an optional skill is missing.

Stricter evidence rules (a review against an independent rater found local and fast models
about 1.4 points too generous, mostly on roles that only share a buzzword with the profile):
- A score of 6 or more needs the day-to-day work itself, not just a keyword, to sit in the
  candidate's core tech stack and domain. A matching technology that is only a side topic
  or a nice-to-have is at most 5.
- A score of 7 or more needs at least two concrete core-stack items named in the
  description, and a role the candidate could start in without a career change.
- Roles whose main work is not hands-on engineering score at most 4: pre-sales, sales
  engineering, solution/enterprise architecture and consulting, field application
  engineering, customer-facing forward-deployed roles, recruiting, project or product
  management, contract code-review or AI-training gigs, and research-scientist roles that
  centre on training new models or a PhD profile.
- A different engineering field that merely uses the same buzzword is at most 5.
- Seniority: Principal, Staff, Head or team-lead scope far above the candidate's
  experience is at most 5 even when the stack matches.
- If the description is missing, empty or just a title/company line, judge from the title
  alone and give at most 5. Never guess requirements that are not written.

---

### EVALUATION STEPS
1. **Extraction:** List the top 5 technical requirements from the Job Description.
2. **Comparison:** Identify which of these the candidate possesses.
3. **Red Flag Check:** Scan for non-matching tech stacks and role types (title penalties come from code).
4. **Scoring:** Calculate the 1-10 score based on weights above.

---

### OUTPUT FORMAT (Strict JSON)
{{
  "ratings": [
    {{
      "job_id": "unique_id_or_number",
      "score": 0,
      "analysis": {{
        "matching_points": ["list specific positive matches"],
        "red_flags": ["list negative keywords or title penalties found"],
        "missing_skills": ["list key requirements from job not in resume"]
      }},
      "verdict": "One sentence summary of fit."
    }}
  ]
}}

### INPUT DATA
JOBS TO EVALUATE:
{jobs_text}"""

        # Call the appropriate API
        if self.api_type == 'deepseek':
            result = self._call_deepseek(prompt)
        elif self.api_type in ['groq','gemini','multi']:
            result = self._call_best_model(prompt)
        else:
            result = None

        if not result:
            for job in jobs:
                job['score'] = 5
                job['match_reasons'] = 'API call failed'
                job['missing_skills'] = ''
            return jobs

        # Parse JSON response
        try:
            json_str = result
            if "```json" in result:
                start = result.find("```json") + 7
                end = result.find("```", start)
                json_str = result[start:end].strip()
            elif "```" in result:
                start = result.find("```") + 3
                end = result.find("```", start)
                json_str = result[start:end].strip()

            payload = json.loads(json_str)
            ratings = payload.get("ratings", [])
            for idx, job in enumerate(jobs):
                if idx < len(ratings):
                    rating = ratings[idx]
                    score = rating.get("score", 5)
                    try:
                        score = int(round(float(score)))
                    except Exception:
                        score = 5
                    score = max(1, min(10, score))
                    if decisions[idx].cap:
                        score = min(score, decisions[idx].cap)
                    if len((job.get('description') or '').strip()) < SPARSE_DESCRIPTION_CHARS:
                        score = min(score, self.sparse_cap)  # no evidence, no match
                    job['score'] = score
                    job['match_reasons'] = rating.get("verdict", "")
                    missing = rating.get("analysis", {}).get("missing_skills", [])
                    if isinstance(missing, list):
                        job['missing_skills'] = ", ".join(missing)
                    else:
                        job['missing_skills'] = str(missing or "")
                else:
                    job['score'] = 5
                    job['match_reasons'] = ''
                    job['missing_skills'] = ''
            return jobs

        except Exception as e:
            print(f"   JSON parse error: {e}")
            for job in jobs:
                job['score'] = 5
                job['match_reasons'] = 'Parse error'
                job['missing_skills'] = ''
            return jobs
    
    def _call_deepseek(self, prompt: str) -> str:
        """Call DeepSeek API."""
        try:
            response = requests.post(
                "https://api.deepseek.com/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "deepseek-chat",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 2000
                },
                timeout=30
            )
            response.raise_for_status()
            return response.json()['choices'][0]['message']['content']
        except Exception as e:
            print(f"   DeepSeek error: {e}")
            return None
    
    def _call_groq(self, prompt: str) -> str:
        """Call Groq API (free Llama)."""
        try:
            api_key = self.config.get('groq_api_key', '')
            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 2000,
                    "temperature": 0.3
                },
                timeout=60
            )
            if response.status_code == 429:
                wait_seconds = int(self.config.get('groq_wait_seconds', 30))
                self._llm_state["groq_cooldown_until"] = time.time() + wait_seconds
                print(f"   Groq rate limit hit. Cooling down {wait_seconds}s...")
                return None
            if response.status_code != 200:
                print(f"   Groq response: {response.text[:200]}")
            response.raise_for_status()
            return response.json()['choices'][0]['message']['content']
        except Exception as e:
            print(f"   Groq error: {e}")
            return None
    
    def _call_gemini(self, prompt: str) -> str:
        """Call Gemini API."""
        try:
            from google import genai
            api_key = self.config.get('gemini_api_key', '')
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            return response.text
        except Exception as e:
            print(f"   Gemini error: {e}")
            if "429" in str(e):
                wait_seconds = int(self.config.get('groq_wait_seconds', 30))
                self._llm_state["gemini_cooldown_until"] = time.time() + wait_seconds
                print(f"   Gemini rate limit hit. Cooling down {wait_seconds}s...")
            return None

    def _call_best_model(self, prompt: str) -> str:
        now = time.time()
        pref = self._llm_state.get("preferred", "groq")
        groq_ready = self.has_groq and now >= self._llm_state.get("groq_cooldown_until", 0)
        gemini_ready = self.has_gemini and now >= self._llm_state.get("gemini_cooldown_until", 0)

        if pref == "groq" and groq_ready:
            res = self._call_groq(prompt)
            if res:
                return res
        if pref == "gemini" and gemini_ready:
            res = self._call_gemini(prompt)
            if res:
                return res

        if groq_ready:
            res = self._call_groq(prompt)
            if res:
                return res
        if gemini_ready:
            res = self._call_gemini(prompt)
            if res:
                return res

        # Both cooling down: wait for earliest
        wait = min(self._llm_state["groq_cooldown_until"], self._llm_state["gemini_cooldown_until"]) - now
        if wait > 0:
            wait_seconds = int(wait)
            print(f"   Both models cooling down. Waiting {wait_seconds}s...")
            time.sleep(wait_seconds)
        return self._call_groq(prompt) or self._call_gemini(prompt)


if __name__ == "__main__":
    print("Job Rater - Supports DeepSeek, Groq, and Gemini")
    print("\nGet free API keys:")
    print("  DeepSeek: https://platform.deepseek.com/api_keys")
    print("  Groq:     https://console.groq.com/keys")
    print("  Gemini:   https://aistudio.google.com/app/apikey")
