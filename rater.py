"""AI Job Rater - Rates jobs 1-10 based on resume match.

Supports cloud providers plus a local LM Studio server using its
OpenAI-compatible API.
"""

import json
import os
import time
from typing import List, Dict

import requests

from language_filter import (
    evidence_appears_in_text,
    language_requirement_excerpts,
    llm_language_rejection,
    normalize_language_assessment,
)


class JobRater:
    """Rate jobs against a candidate profile with a cloud or local LLM."""
    
    def __init__(self, config: dict, candidate_profile: dict):
        self.candidate_profile = candidate_profile
        self.config = config
        
        # Determine which API to use
        self.api_type = None
        self.api_key = None

        self.has_groq = bool(config.get('groq_api_key', '').startswith('gsk_'))
        self.has_gemini = bool(config.get('gemini_api_key', '').startswith('AIza'))
        self.has_deepseek = bool(config.get('deepseek_api_key', '').startswith('sk-'))
        self.has_lmstudio = bool(config.get('lmstudio_enabled', False))
        self.lmstudio_api_base = str(
            os.getenv('LMSTUDIO_API_BASE')
            or config.get('lmstudio_api_base', 'http://localhost:1234/v1')
        ).rstrip('/')
        if self.lmstudio_api_base.endswith('/v1'):
            self.lmstudio_native_api_base = self.lmstudio_api_base[:-3] + '/api/v1'
        else:
            self.lmstudio_native_api_base = self.lmstudio_api_base + '/api/v1'
        self.lmstudio_model = str(
            config.get('lmstudio_model', 'gemma-4-26b-a4b-it-qat')
        )
        self.lmstudio_timeout_seconds = int(config.get('lmstudio_timeout_seconds', 180))
        self.llm_language_gate_enabled = bool(config.get('llm_language_gate_enabled', False))
        self.candidate_german_level = str(config.get('candidate_german_level', 'A2')).upper()
        self.reject_any_mandatory_german = bool(config.get('reject_any_mandatory_german', True))

        preferred = config.get("rating_preferred", "groq")
        if self.has_lmstudio and preferred == 'lmstudio':
            self.api_type = 'lmstudio'
            print(f"   Using local LM Studio model: {self.lmstudio_model}")
        elif self.has_groq and self.has_gemini:
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
            "preferred": preferred,
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

        # The local model has an 8K context window.  Sending one job at a time
        # keeps the candidate profile, prompt, and response safely within it.
        if self.api_type == 'lmstudio':
            batch_size = max(1, int(self.config.get('lmstudio_batch_size', 1)))
        
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
        candidate_json = json.dumps(self.candidate_profile, ensure_ascii=False)
        profile_rules = json.dumps({
            "core_tech": self.candidate_profile.get("candidate_profile", {}).get("core_tech", []),
            "positive_keywords": self.candidate_profile.get("matching_rules", {}).get("positive_keywords", []),
            "negative_title_keywords": self.candidate_profile.get("matching_rules", {}).get("negative_title_keywords", []),
            "negative_description_keywords": self.candidate_profile.get("matching_rules", {}).get("negative_description_keywords", []),
            "scoring_protocol": self.candidate_profile.get("scoring_protocol", {}),
        }, ensure_ascii=False)
        language_policy = json.dumps({
            "enabled": self.llm_language_gate_enabled,
            "candidate_german_level": self.candidate_german_level,
            "reject_any_mandatory_german": self.reject_any_mandatory_german,
        }, ensure_ascii=False)

        job_blocks = []
        max_description_chars = max(
            500, int(self.config.get("rating_description_max_chars", 3500))
        )
        for idx, job in enumerate(jobs, 1):
            full_desc = (job.get("description") or "").strip()
            desc = full_desc
            if len(desc) > max_description_chars:
                desc = desc[:max_description_chars] + "..."
            block = "\n".join([
                f"JOB {idx}",
                f"Title: {job.get('title','')}",
                f"Company: {job.get('company','')}",
                f"Location: {job.get('location','')}",
                f"URL: {job.get('url','')}",
                f"Description: {desc}",
                "German/Deutsch context from full description: " + (
                    " | ".join(language_requirement_excerpts(full_desc)) or "None found"
                ),
            ])
            job_blocks.append(block)
        jobs_text = "\n\n".join(job_blocks)

        prompt = f"""### ROLE
    Expert Technical Talent Matcher.

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
Use this profile-derived ruleset as the only scoring authority:
{profile_rules}

Language policy:
{language_policy}

Apply hard title penalties only when a negative title keyword appears in the
    job title. Treat negative description keywords as description penalties, not
    as automatic rejection unless the profile's scoring protocol says so.

Score using this explicit calibration:
- 9-10: Strong match for the required embedded stack and domain, with only minor gaps.
- 7-8: Good match with the main stack present and manageable missing skills.
- 5-6: Partial match; some relevant skills but important requirements are missing.
- 3-4: Weak match with limited technical overlap.
- 1-2: Hard title penalty or clearly incompatible role/domain.
Do not award a high score from a generic software title alone. Do not reduce a
score merely because an optional skill is missing. Base the score on evidence
in the title and description, and keep the score within the requested range.

---

### EVALUATION STEPS
1. **Extraction:** List the top 5 technical requirements from the Job Description.
2. **Comparison:** Identify which of these the candidate possesses.
3. **Red Flag Check:** Scan only the profile-derived title and description rules.
4. **German Requirement:** Classify explicit German-language proficiency as
   mandatory, optional, not_mentioned, or unclear. Do not infer it from the
   country, a German company, market, customers, team, or posting language.
   Copy one exact supporting quote as evidence, or use an empty string.
5. **Scoring:** Calculate the 1-10 score based on weights above.

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
      "language_assessment": {{
        "german_requirement": "mandatory|optional|not_mentioned|unclear",
        "required_level": "A1|A2|B1|B2|C1|C2|unspecified",
        "evidence": "short exact quote or empty string"
      }},
      "verdict": "One sentence summary of fit."
    }}
  ]
}}

### INPUT DATA
JOBS TO EVALUATE:
{jobs_text}"""

        # Call the appropriate API
        if self.api_type == 'lmstudio':
            result = self._call_lmstudio(prompt)
        elif self.api_type == 'deepseek':
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

            # Local models occasionally prepend a short explanation despite the
            # JSON-only instruction. Decode the first JSON object so that a
            # Markdown fence or trailing prose does not discard a valid rating.
            try:
                payload = json.loads(json_str)
            except json.JSONDecodeError:
                object_start = json_str.find("{")
                if object_start < 0:
                    raise
                payload, _ = json.JSONDecoder().raw_decode(json_str[object_start:])
            ratings = payload.get("ratings", [])
            if not isinstance(ratings, list) or not ratings:
                raise ValueError("response contains no ratings")
            for idx, job in enumerate(jobs):
                if idx < len(ratings):
                    rating = ratings[idx]
                    score = rating.get("score", 5)
                    try:
                        score = int(round(float(score)))
                    except Exception:
                        score = 5
                    score = max(1, min(10, score))
                    job['score'] = score
                    job['match_reasons'] = rating.get("verdict", "")
                    missing = rating.get("analysis", {}).get("missing_skills", [])
                    if isinstance(missing, list):
                        job['missing_skills'] = ", ".join(missing)
                    else:
                        job['missing_skills'] = str(missing or "")
                    assessment = normalize_language_assessment(rating.get("language_assessment", {}))
                    if assessment['german_requirement'] == 'mandatory' and not evidence_appears_in_text(
                        assessment['evidence'], job.get('description', '')
                    ):
                        assessment['german_requirement'] = 'unclear'
                        assessment['evidence'] = ''
                    job['german_requirement'] = assessment['german_requirement']
                    job['german_required_level'] = assessment['required_level']
                    job['german_requirement_evidence'] = assessment['evidence']
                    rejection = None
                    if self.llm_language_gate_enabled:
                        rejection = llm_language_rejection(
                            assessment, self.candidate_german_level, self.reject_any_mandatory_german
                        )
                    job['language_eligible'] = False if rejection else (
                        None if assessment['german_requirement'] == 'unclear' else True
                    )
                    job['language_rejection_reason'] = rejection or ''
                else:
                    job['score'] = 5
                    job['match_reasons'] = ''
                    job['missing_skills'] = ''
            return jobs

        except Exception as e:
            print(f"   JSON parse error: {e}")
            print(f"   LM response preview: {result[:500]!r}")
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

    def _call_lmstudio(self, prompt: str) -> str:
        """Call LM Studio's native chat endpoint with reasoning disabled."""
        try:
            response = requests.post(
                f"{self.lmstudio_native_api_base}/chat",
                json={
                    "model": self.lmstudio_model,
                    "system_prompt": (
                        "Return only the strict JSON requested by the user. "
                        "Do not wrap it in prose, Markdown, or reasoning."
                    ),
                    "input": prompt,
                    "temperature": 0.1,
                    "max_output_tokens": int(self.config.get('lmstudio_max_tokens', 1200)),
                    "reasoning": self.config.get('lmstudio_reasoning', 'off'),
                    "store": False,
                },
                timeout=self.lmstudio_timeout_seconds,
            )
            if response.status_code != 200:
                print(f"   LM Studio response: {response.text[:300]}")
            response.raise_for_status()
            output = response.json().get('output', [])
            content = [
                item.get('content', '')
                for item in output
                if item.get('type') == 'message' and item.get('content')
            ]
            if not content:
                print("   LM Studio response contained no final message.")
                return None
            return "\n".join(content)
        except Exception as e:
            print(f"   LM Studio error: {e}")
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
