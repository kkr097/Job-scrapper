"""
AI Job Rater - Rates jobs 1-10 based on resume match.
Supports: DeepSeek (free), Gemini, Groq, or local Ollama.
"""

import json
import time
import os
from typing import List, Dict

import requests


class JobRater:
    """Rate jobs against resume using AI (DeepSeek, Gemini, or Groq)."""
    
    def __init__(self, config: dict, resume_summary: str):
        self.resume_summary = resume_summary
        self.config = config
        
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
                
                # Rate limit delay
                if i + batch_size < len(jobs):
                    if self.api_type in ['gemini', 'groq', 'multi']:
                        wait_seconds = int(self.config.get('groq_wait_seconds', 30))
                        print(f"   Waiting {wait_seconds}s to avoid rate limits...")
                        time.sleep(wait_seconds)
                    else:
                        time.sleep(1.5)
                    
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
        
        # Build job list (compact format)
        job_list = ""
        for idx, job in enumerate(jobs, 1):
            job_list += f"{idx}. {job['title']} at {job['company']}\n"
        
        # Use a shorter resume summary to fit token limits
        short_summary = self.resume_summary[:1500] if len(self.resume_summary) > 1500 else self.resume_summary
        
        prompt = f"""Rate these jobs against the candidate and keyword rules.

Candidate summary:
{short_summary}

Positive keywords (should match): {', '.join(self.config.get('include_description_keywords', []))}
Negative keywords (should penalize): {', '.join(self.config.get('exclude_description_keywords', []))}
Negative title keywords (should penalize if present): {', '.join(self.config.get('exclude_keywords', []))}

JOBS:
{job_list}
Score 1-10 (10=strong match, 1=wrong field).

Return ONLY JSON:
{{"ratings": [{{"job": 1, "score": 8, "reasons": "keyword match", "missing": "missing key skills"}}, {{"job": 2, "score": 3, "reasons": "negative keywords", "missing": "wrong field"}}]}}"""

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
            
            ratings = json.loads(json_str)
            
            for rating in ratings.get('ratings', []):
                idx = rating.get('job', 1) - 1
                if 0 <= idx < len(jobs):
                    jobs[idx]['score'] = rating.get('score', 5)
                    jobs[idx]['match_reasons'] = rating.get('reasons', '')
                    jobs[idx]['missing_skills'] = rating.get('missing', '')
            
            for job in jobs:
                if 'score' not in job:
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
