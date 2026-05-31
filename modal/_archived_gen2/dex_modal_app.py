import asyncio
import json
import os
import time
from typing import Any

import httpx
import modal
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

app = modal.App("data-engine-x-micro")

image = modal.Image.debian_slim().pip_install("fastapi", "httpx")
auth_scheme = HTTPBearer(auto_error=False)
parallel_base_url = "https://api.parallel.ai/v1/tasks/runs"

# -----------------------------------------------------------------------------
# Task Specs
# -----------------------------------------------------------------------------

task_spec_find_linkedin_url_by_domain = {
    "input_schema": {
        "json_schema": {
            "properties": {
                "company_domain": {
                    "description": "The domain of the company to find the LinkedIn URL for",
                    "type": "string",
                }
            },
            "type": "object",
        },
        "type": "json",
    },
    "output_schema": {
        "json_schema": {
            "additionalProperties": False,
            "properties": {
                "linkedin_url": {
                    "description": (
                        "The official LinkedIn profile URL for the company. "
                        "If a LinkedIn profile cannot be found or verified, return null."
                    ),
                    "type": "string",
                }
            },
            "required": ["linkedin_url"],
            "type": "object",
        },
        "type": "json",
    },
}

task_spec_find_linkedin_url_by_name_and_domain = {
    "input_schema": {
        "json_schema": {
            "properties": {
                "company_domain": {
                    "description": "The domain of the company to find the LinkedIn URL for.",
                    "type": "string",
                },
                "company_name": {
                    "description": "The name of the company to find the LinkedIn URL for.",
                    "type": "string",
                },
            },
            "type": "object",
        },
        "type": "json",
    },
    "output_schema": {
        "json_schema": {
            "additionalProperties": False,
            "properties": {
                "company_linkedin_url": {
                    "description": "The official LinkedIn profile URL for the company. If a LinkedIn profile cannot be found for the company, return null.",
                    "type": "string",
                }
            },
            "required": ["company_linkedin_url"],
            "type": "object",
        },
        "type": "json",
    },
}

task_spec_find_company_name_by_domain = {
    "input_schema": {
        "json_schema": {
            "properties": {
                "domain": {
                    "description": "The domain name of the company",
                    "type": "string",
                }
            },
            "type": "object",
        },
        "type": "json",
    },
    "output_schema": {
        "json_schema": {
            "additionalProperties": False,
            "properties": {
                "company_name": {
                    "description": "The official name of the company associated with the provided domain. If the company name cannot be determined from the domain, return null.",
                    "type": "string",
                }
            },
            "required": ["company_name"],
            "type": "object",
        },
        "type": "json",
    },
}

task_spec_find_company_name_by_linkedin_url = {
    "input_schema": {
        "json_schema": {
            "properties": {
                "company_linkedin_url": {
                    "description": "The LinkedIn URL of the company to extract the name from.",
                    "type": "string",
                }
            },
            "type": "object",
        },
        "type": "json",
    },
    "output_schema": {
        "json_schema": {
            "additionalProperties": False,
            "properties": {
                "company_name": {
                    "description": "The official name of the company as listed on its LinkedIn profile. If the company name cannot be extracted or is unavailable, return null.",
                    "type": "string",
                }
            },
            "required": ["company_name"],
            "type": "object",
        },
        "type": "json",
    },
}

task_spec_find_company_domain_by_linkedin_url = {
    "input_schema": {
        "json_schema": {
            "properties": {
                "linkedin_url": {
                    "description": "The LinkedIn URL of the company to find the domain for.",
                    "type": "string",
                }
            },
            "type": "object",
        },
        "type": "json",
    },
    "output_schema": {
        "json_schema": {
            "additionalProperties": False,
            "properties": {
                "company_domain": {
                    "description": "The primary domain name of the company (e.g., 'example.com') derived from the provided LinkedIn URL. If the domain cannot be extracted or is unavailable, return null.",
                    "type": "string",
                }
            },
            "required": ["company_domain"],
            "type": "object",
        },
        "type": "json",
    },
}

task_spec_find_company_domain_by_name_and_linkedin_url = {
    "input_schema": {
        "json_schema": {
            "properties": {
                "company_linkedin_url": {
                    "description": "The LinkedIn URL of the company to find the domain for.",
                    "type": "string",
                },
                "company_name": {
                    "description": "The name of the company to find the domain for.",
                    "type": "string",
                },
            },
            "type": "object",
        },
        "type": "json",
    },
    "output_schema": {
        "json_schema": {
            "additionalProperties": False,
            "properties": {
                "company_domain": {
                    "description": "The official domain for the company. If the domain cannot be found, return null.",
                    "type": "string",
                }
            },
            "required": ["company_domain"],
            "type": "object",
        },
        "type": "json",
    },
}

task_spec_find_company_description_by_domain = {
    "input_schema": {
        "json_schema": {
            "properties": {
                "company_domain": {
                    "description": "The domain of the company to retrieve its description",
                    "type": "string",
                }
            },
            "type": "object",
        },
        "type": "json",
    },
    "output_schema": {
        "json_schema": {
            "additionalProperties": False,
            "properties": {
                "company_description": {
                    "description": "A 2-4 sentence summary providing a high-level overview of the company's primary business activities, products, services, and mission, derived from information available on the company's official website or reliable public sources. If a description cannot be found, return 'Description unavailable'.",
                    "type": "string",
                }
            },
            "required": ["company_description"],
            "type": "object",
        },
        "type": "json",
    },
}

task_spec_find_company_description_by_name_and_domain = {
    "input_schema": {
        "json_schema": {
            "properties": {
                "company_name": {
                    "description": "The name of the company to find the description for.",
                    "type": "string",
                },
                "company_domain": {
                    "description": "The domain of the company to find the description for.",
                    "type": "string",
                },
            },
            "type": "object",
        },
        "type": "json",
    },
    "output_schema": {
        "json_schema": {
            "additionalProperties": False,
            "properties": {
                "company_description": {
                    "description": "A 1-3 sentence summary describing the company's primary business activities, mission, and offerings, based on information found on its official website or reputable business directories. If a description cannot be found, return 'Description unavailable'.",
                    "type": "string",
                }
            },
            "required": ["company_description"],
            "type": "object",
        },
        "type": "json",
    },
}

task_spec_find_company_hq_location_by_domain = {
    "input_schema": {
        "json_schema": {
            "properties": {
                "company_domain": {
                    "description": "The domain of the company to find the HQ location for.",
                    "type": "string",
                }
            },
            "type": "object",
        },
        "type": "json",
    },
    "output_schema": {
        "json_schema": {
            "additionalProperties": False,
            "properties": {
                "hq_city": {
                    "description": "The city where the company's headquarters is located. If unavailable, return null.",
                    "type": "string",
                },
                "hq_country": {
                    "description": "The country where the company's headquarters is located. If unavailable, return null.",
                    "type": "string",
                },
                "hq_state": {
                    "description": "The state or province where the company's headquarters is located. If unavailable, return null.",
                    "type": "string",
                },
            },
            "required": ["hq_city", "hq_state", "hq_country"],
            "type": "object",
        },
        "type": "json",
    },
}

task_spec_find_company_hq_location_by_name_and_domain = {
    "input_schema": {
        "json_schema": {
            "properties": {
                "company_name": {
                    "description": "The name of the company to find the HQ location for.",
                    "type": "string",
                },
                "company_domain": {
                    "description": "The domain of the company to find the HQ location for.",
                    "type": "string",
                },
            },
            "type": "object",
        },
        "type": "json",
    },
    "output_schema": {
        "json_schema": {
            "additionalProperties": False,
            "properties": {
                "hq_city": {
                    "description": "The city where the company's headquarters is located. If the city cannot be determined, return null.",
                    "type": "string",
                },
                "hq_country": {
                    "description": "The country where the company's headquarters is located. If the country cannot be determined, return null.",
                    "type": "string",
                },
                "hq_state": {
                    "description": "The state, province, or region where the company's headquarters is located. If the state, province, or region cannot be determined, return null.",
                    "type": "string",
                },
            },
            "required": ["hq_city", "hq_state", "hq_country"],
            "type": "object",
        },
        "type": "json",
    },
}

task_spec_find_company_employee_range_by_name_domain_and_linkedin_url = {
    "input_schema": {
        "json_schema": {
            "properties": {
                "company_domain": {
                    "description": "The domain of the company to find the employee range for.",
                    "type": "string",
                },
                "company_linkedin_url": {
                    "description": "The LinkedIn URL of the company to find the employee range for.",
                    "type": "string",
                },
                "company_name": {
                    "description": "The name of the company to find the employee range for.",
                    "type": "string",
                },
            },
            "type": "object",
        },
        "type": "json",
    },
    "output_schema": {
        "json_schema": {
            "additionalProperties": False,
            "properties": {
                "employee_range": {
                    "description": "The estimated number of employees working at the company, represented as a range (e.g., '1-10', '11-50', '51-200', '201-500', '501-1000', '1001-5000', '5001-10000', '10000+'). If an exact number is found, convert it to the appropriate range. If the employee range cannot be determined, return 'Unknown'.",
                    "type": "string",
                }
            },
            "required": ["employee_range"],
            "type": "object",
        },
        "type": "json",
    },
}

# Person Task Specs

task_spec_find_person_linkedin_url_by_full_name_company_name_and_company_domain = {
    "input_schema": {
        "json_schema": {
            "properties": {
                "company_name": {
                    "description": "The name of the company the person works for.",
                    "type": "string",
                },
                "company_domain": {
                    "description": "The domain of the company the person works for.",
                    "type": "string",
                },
                "full_name": {
                    "description": "The full name of the person whose LinkedIn URL needs to be found.",
                    "type": "string",
                },
            },
            "type": "object",
        },
        "type": "json",
    },
    "output_schema": {
        "json_schema": {
            "additionalProperties": False,
            "properties": {
                "linkedin_url": {
                    "description": "The direct URL to the LinkedIn profile of the person identified by the provided full name, company name, and company domain. If a LinkedIn profile cannot be found, return null.",
                    "type": "string",
                }
            },
            "required": ["linkedin_url"],
            "type": "object",
        },
        "type": "json",
    },
}

task_spec_find_person_linkedin_url_by_name_and_company = {
    "input_schema": {
        "json_schema": {
            "properties": {
                "company_name": {
                    "description": "The name of the company the person works for.",
                    "type": "string",
                },
                "person_name": {
                    "description": "The name of the person to find the LinkedIn URL for.",
                    "type": "string",
                },
            },
            "type": "object",
        },
        "type": "json",
    },
    "output_schema": {
        "json_schema": {
            "additionalProperties": False,
            "properties": {
                "linkedin_url": {
                    "description": "The direct URL to the LinkedIn profile of the person identified by the provided name and company. If a LinkedIn profile cannot be found for the specified person and company, return null.",
                    "type": "string",
                }
            },
            "required": ["linkedin_url"],
            "type": "object",
        },
        "type": "json",
    },
}

task_spec_find_person_work_email_by_full_name_company_name_company_domain_and_linkedin_url = {
    "input_schema": {
        "json_schema": {
            "properties": {
                "company_name": {
                    "description": "The name of the company where the person works.",
                    "type": "string",
                },
                "company_domain": {
                    "description": "The domain of the company where the person works.",
                    "type": "string",
                },
                "person_full_name": {
                    "description": "The full name of the person whose work email needs to be found.",
                    "type": "string",
                },
                "person_linkedin_url": {
                    "description": "The LinkedIn profile URL of the person.",
                    "type": "string",
                },
            },
            "type": "object",
        },
        "type": "json",
    },
    "output_schema": {
        "json_schema": {
            "additionalProperties": False,
            "properties": {
                "work_email": {
                    "description": "The professional email address of the person at the specified company. If the work email cannot be found, return null.",
                    "type": "string",
                }
            },
            "required": ["work_email"],
            "type": "object",
        },
        "type": "json",
    },
}

task_spec_find_work_email_by_full_name_company_name_and_company_domain = {
    "input_schema": {
        "json_schema": {
            "properties": {
                "company_name": {
                    "description": "The name of the company where the person works.",
                    "type": "string",
                },
                "company_domain": {
                    "description": "The domain of the company where the person works.",
                    "type": "string",
                },
                "full_name": {
                    "description": "The full name of the person whose work email needs to be found.",
                    "type": "string",
                },
            },
            "type": "object",
        },
        "type": "json",
    },
    "output_schema": {
        "json_schema": {
            "additionalProperties": False,
            "properties": {
                "work_email": {
                    "description": "The professional email address of the person at the specified company. If the email address cannot be found or verified, return null.",
                    "type": "string",
                }
            },
            "required": ["work_email"],
            "type": "object",
        },
        "type": "json",
    },
}

task_spec_find_person_email_by_name_and_company = {
    "input_schema": {
        "json_schema": {
            "properties": {
                "company_name": {
                    "description": "The name of the company where the person works.",
                    "type": "string",
                },
                "person_full_name": {
                    "description": "The full name of the person whose email needs to be found.",
                    "type": "string",
                },
            },
            "type": "object",
        },
        "type": "json",
    },
    "output_schema": {
        "json_schema": {
            "additionalProperties": False,
            "properties": {
                "person_email": {
                    "description": "The professional email address of the person at the specified company. If the email address cannot be found, return null.",
                    "type": "string",
                }
            },
            "required": ["person_email"],
            "type": "object",
        },
        "type": "json",
    },
}

task_spec_find_person_email_and_linkedin_url_by_full_name_and_company_name = {
    "input_schema": {
        "json_schema": {
            "properties": {
                "company_name": {
                    "description": "The name of the company where the person works.",
                    "type": "string",
                },
                "full_name": {
                    "description": "The full name of the person to find the work mail and LinkedIn URL for.",
                    "type": "string",
                },
            },
            "type": "object",
        },
        "type": "json",
    },
    "output_schema": {
        "json_schema": {
            "additionalProperties": False,
            "properties": {
                "linkedin_url": {
                    "description": "The URL to the person's LinkedIn profile. If unavailable, return null.",
                    "type": "string",
                },
                "work_mail": {
                    "description": "The professional email address of the person at the specified company. If unavailable, return null.",
                    "type": "string",
                },
            },
            "required": ["work_mail", "linkedin_url"],
            "type": "object",
        },
        "type": "json",
    },
}

task_spec_find_person_work_email_and_linkedin_url_by_full_name_company_name_and_company_domain = {
    "input_schema": {
        "json_schema": {
            "properties": {
                "company_name": {
                    "description": "The name of the company the person works for.",
                    "type": "string",
                },
                "company_domain": {
                    "description": "The domain of the company the person works for.",
                    "type": "string",
                },
                "full_name": {
                    "description": "The full name of the person to find the work email and LinkedIn URL for.",
                    "type": "string",
                },
            },
            "type": "object",
        },
        "type": "json",
    },
    "output_schema": {
        "json_schema": {
            "additionalProperties": False,
            "properties": {
                "linkedin_url": {
                    "description": "The URL to the person's professional LinkedIn profile. If the LinkedIn URL cannot be found, return null.",
                    "type": "string",
                },
                "work_email": {
                    "description": "The professional email address of the person at the specified company. If the work email cannot be found, return null.",
                    "type": "string",
                },
            },
            "required": ["work_email", "linkedin_url"],
            "type": "object",
        },
        "type": "json",
    },
}

task_spec_find_person_location_by_full_name_and_linkedin_url = {
    "input_schema": {
        "json_schema": {
            "properties": {
                "person_full_name": {
                    "description": "The full name of the person to find the living location for.",
                    "type": "string",
                },
                "person_linkedin_url": {
                    "description": "The LinkedIn profile URL of the person to find the living location for.",
                    "type": "string",
                },
            },
            "type": "object",
        },
        "type": "json",
    },
    "output_schema": {
        "json_schema": {
            "additionalProperties": False,
            "properties": {
                "city": {
                    "description": "The city where the person lives. If unavailable, return null.",
                    "type": "string",
                },
                "country": {
                    "description": "The country where the person lives. If unavailable, return null.",
                    "type": "string",
                },
                "state": {
                    "description": "The state or province where the person lives. If unavailable, return null.",
                    "type": "string",
                },
            },
            "required": ["city", "state", "country"],
            "type": "object",
        },
        "type": "json",
    },
}


# -----------------------------------------------------------------------------
# Request Models
# -----------------------------------------------------------------------------


class CompanyDomainRequest(BaseModel):
    company_domain: str


class DomainRequest(BaseModel):
    domain: str


class CompanyNameAndDomainRequest(BaseModel):
    company_name: str
    company_domain: str


class LinkedInUrlRequest(BaseModel):
    linkedin_url: str


class CompanyLinkedInUrlRequest(BaseModel):
    company_linkedin_url: str


class CompanyNameAndLinkedInUrlRequest(BaseModel):
    company_name: str
    company_linkedin_url: str


class CompanyNameDomainAndLinkedInUrlRequest(BaseModel):
    company_name: str
    company_domain: str
    company_linkedin_url: str


class PersonNameAndCompanyRequest(BaseModel):
    person_name: str
    company_name: str


class FullNameAndCompanyNameRequest(BaseModel):
    full_name: str
    company_name: str


class FullNameCompanyNameAndDomainRequest(BaseModel):
    full_name: str
    company_name: str
    company_domain: str


class PersonFullNameAndCompanyRequest(BaseModel):
    person_full_name: str
    company_name: str


class PersonFullNameCompanyNameDomainAndLinkedInUrlRequest(BaseModel):
    person_full_name: str
    company_name: str
    company_domain: str
    person_linkedin_url: str


class PersonFullNameAndLinkedInUrlRequest(BaseModel):
    person_full_name: str
    person_linkedin_url: str


def _deep_find_first(data: Any, keys: set[str]) -> Any:
    if isinstance(data, dict):
        for key, value in data.items():
            if key in keys:
                return value
            nested = _deep_find_first(value, keys)
            if nested is not None:
                return nested
    elif isinstance(data, list):
        for item in data:
            nested = _deep_find_first(item, keys)
            if nested is not None:
                return nested
    return None


def _as_output_dict(raw_output: Any) -> dict[str, Any] | None:
    if isinstance(raw_output, dict):
        return raw_output
    if isinstance(raw_output, str):
        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _extract_parallel_output(response_body: Any) -> dict[str, Any] | None:
    output_candidate = _deep_find_first(response_body, {"output", "result", "data"})
    # Parallel JSON outputs commonly arrive as {"type":"json","content":{...}}.
    if isinstance(output_candidate, dict):
        content_value = output_candidate.get("content")
        if isinstance(content_value, dict):
            return content_value
    parsed_output = _as_output_dict(output_candidate)
    if parsed_output is not None:
        return parsed_output
    if isinstance(response_body, dict) and "linkedin_url" in response_body:
        return response_body
    return None


def _extract_error_message(response_body: Any) -> str | None:
    message = _deep_find_first(response_body, {"error", "message", "detail"})
    if isinstance(message, str) and message.strip():
        return message.strip()
    return None


POLL_INTERVAL_SECONDS = 2
POLL_TIMEOUT_SECONDS = 120


async def run_parallel_task(
    *,
    task_spec: dict[str, Any],
    input_data: dict[str, Any],
    processor: str,
) -> dict[str, Any]:
    api_key = os.environ.get("PARALLEL_API_KEY")
    if not api_key:
        raise RuntimeError("PARALLEL_API_KEY is not configured")

    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "input": input_data,
        "processor": processor,
        "task_spec": task_spec,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Create the task run
        create_response = await client.post(parallel_base_url, headers=headers, json=payload)
        if create_response.status_code >= 400:
            raise RuntimeError(f"Parallel.ai POST failed: {create_response.status_code}")

        create_body = create_response.json()

        # Check if result is inline (not queued)
        status = create_body.get("status")
        if status not in ("queued", "running", "pending"):
            output = _extract_parallel_output(create_body)
            if output is not None:
                return output
            raise RuntimeError(f"No output in response: {create_body}")

        # Need to poll for result
        run_id = create_body.get("run_id")
        if not run_id:
            raise RuntimeError(f"No run_id in queued response: {create_body}")

        poll_url = f"{parallel_base_url}/{run_id}"
        start_time = time.time()

        while time.time() - start_time < POLL_TIMEOUT_SECONDS:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

            poll_response = await client.get(poll_url, headers={"x-api-key": api_key})
            if poll_response.status_code >= 400:
                raise RuntimeError(f"Parallel.ai poll failed: {poll_response.status_code}")

            poll_body = poll_response.json()
            poll_status = poll_body.get("status")

            if poll_status in ("completed", "succeeded"):
                # Fetch the actual result from the result endpoint
                result_url = f"{parallel_base_url}/{run_id}/result"
                result_response = await client.get(result_url, headers={"x-api-key": api_key})
                if result_response.status_code >= 400:
                    raise RuntimeError(f"Parallel.ai result fetch failed: {result_response.status_code}")
                result_body = result_response.json()
                output = _extract_parallel_output(result_body)
                if output is not None:
                    return output
                raise RuntimeError(f"No output in result response: {result_body}")

            if poll_status in ("failed", "error"):
                error_msg = _extract_error_message(poll_body) or "Task failed"
                raise RuntimeError(error_msg)

            # Still queued/running, continue polling

        raise RuntimeError(f"Parallel.ai task timed out after {POLL_TIMEOUT_SECONDS}s")


def require_internal_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(auth_scheme),
) -> None:
    expected_key = os.environ.get("MODAL_INTERNAL_AUTH_KEY")
    if not expected_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="MODAL_INTERNAL_AUTH_KEY is not configured",
        )

    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or credentials.credentials != expected_key
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )


web_app = FastAPI(
    title="data-engine-x-micro",
    dependencies=[Depends(require_internal_auth)],
)


@web_app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


# -----------------------------------------------------------------------------
# Company Endpoints
# -----------------------------------------------------------------------------


@web_app.post("/company/find-linkedin-url-by-domain")
async def find_linkedin_url_by_domain(payload: CompanyDomainRequest) -> dict[str, Any]:
    company_domain = payload.company_domain.strip()
    if not company_domain:
        return {"success": False, "error": "company_domain is required"}

    try:
        output = await run_parallel_task(
            task_spec=task_spec_find_linkedin_url_by_domain,
            input_data={"company_domain": company_domain},
            processor="base",
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    linkedin_url = output.get("linkedin_url") if isinstance(output, dict) else None
    return {"success": True, "data": {"linkedin_url": linkedin_url}}


@web_app.post("/company/find-linkedin-url-by-name-and-domain")
async def find_linkedin_url_by_name_and_domain(payload: CompanyNameAndDomainRequest) -> dict[str, Any]:
    company_name = payload.company_name.strip()
    company_domain = payload.company_domain.strip()
    if not company_name or not company_domain:
        return {"success": False, "error": "company_name and company_domain are required"}

    try:
        output = await run_parallel_task(
            task_spec=task_spec_find_linkedin_url_by_name_and_domain,
            input_data={"company_name": company_name, "company_domain": company_domain},
            processor="base",
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    company_linkedin_url = output.get("company_linkedin_url") if isinstance(output, dict) else None
    return {"success": True, "data": {"company_linkedin_url": company_linkedin_url}}


@web_app.post("/company/find-name-by-domain")
async def find_company_name_by_domain(payload: DomainRequest) -> dict[str, Any]:
    domain = payload.domain.strip()
    if not domain:
        return {"success": False, "error": "domain is required"}

    try:
        output = await run_parallel_task(
            task_spec=task_spec_find_company_name_by_domain,
            input_data={"domain": domain},
            processor="lite",
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    company_name = output.get("company_name") if isinstance(output, dict) else None
    return {"success": True, "data": {"company_name": company_name}}


@web_app.post("/company/find-name-by-linkedin-url")
async def find_company_name_by_linkedin_url(payload: CompanyLinkedInUrlRequest) -> dict[str, Any]:
    company_linkedin_url = payload.company_linkedin_url.strip()
    if not company_linkedin_url:
        return {"success": False, "error": "company_linkedin_url is required"}

    try:
        output = await run_parallel_task(
            task_spec=task_spec_find_company_name_by_linkedin_url,
            input_data={"company_linkedin_url": company_linkedin_url},
            processor="lite",
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    company_name = output.get("company_name") if isinstance(output, dict) else None
    return {"success": True, "data": {"company_name": company_name}}


@web_app.post("/company/find-domain-by-linkedin-url")
async def find_company_domain_by_linkedin_url(payload: LinkedInUrlRequest) -> dict[str, Any]:
    linkedin_url = payload.linkedin_url.strip()
    if not linkedin_url:
        return {"success": False, "error": "linkedin_url is required"}

    try:
        output = await run_parallel_task(
            task_spec=task_spec_find_company_domain_by_linkedin_url,
            input_data={"linkedin_url": linkedin_url},
            processor="base",
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    company_domain = output.get("company_domain") if isinstance(output, dict) else None
    return {"success": True, "data": {"company_domain": company_domain}}


@web_app.post("/company/find-domain-by-name-and-linkedin-url")
async def find_company_domain_by_name_and_linkedin_url(payload: CompanyNameAndLinkedInUrlRequest) -> dict[str, Any]:
    company_name = payload.company_name.strip()
    company_linkedin_url = payload.company_linkedin_url.strip()
    if not company_name or not company_linkedin_url:
        return {"success": False, "error": "company_name and company_linkedin_url are required"}

    try:
        output = await run_parallel_task(
            task_spec=task_spec_find_company_domain_by_name_and_linkedin_url,
            input_data={"company_name": company_name, "company_linkedin_url": company_linkedin_url},
            processor="base",
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    company_domain = output.get("company_domain") if isinstance(output, dict) else None
    return {"success": True, "data": {"company_domain": company_domain}}


@web_app.post("/company/find-description-by-domain")
async def find_company_description_by_domain(payload: CompanyDomainRequest) -> dict[str, Any]:
    company_domain = payload.company_domain.strip()
    if not company_domain:
        return {"success": False, "error": "company_domain is required"}

    try:
        output = await run_parallel_task(
            task_spec=task_spec_find_company_description_by_domain,
            input_data={"company_domain": company_domain},
            processor="lite",
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    company_description = output.get("company_description") if isinstance(output, dict) else None
    return {"success": True, "data": {"company_description": company_description}}


@web_app.post("/company/find-description-by-name-and-domain")
async def find_company_description_by_name_and_domain(payload: CompanyNameAndDomainRequest) -> dict[str, Any]:
    company_name = payload.company_name.strip()
    company_domain = payload.company_domain.strip()
    if not company_name or not company_domain:
        return {"success": False, "error": "company_name and company_domain are required"}

    try:
        output = await run_parallel_task(
            task_spec=task_spec_find_company_description_by_name_and_domain,
            input_data={"company_name": company_name, "company_domain": company_domain},
            processor="base",
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    company_description = output.get("company_description") if isinstance(output, dict) else None
    return {"success": True, "data": {"company_description": company_description}}


@web_app.post("/company/find-hq-location-by-domain")
async def find_company_hq_location_by_domain(payload: CompanyDomainRequest) -> dict[str, Any]:
    company_domain = payload.company_domain.strip()
    if not company_domain:
        return {"success": False, "error": "company_domain is required"}

    try:
        output = await run_parallel_task(
            task_spec=task_spec_find_company_hq_location_by_domain,
            input_data={"company_domain": company_domain},
            processor="lite",
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    if isinstance(output, dict):
        return {
            "success": True,
            "data": {
                "hq_city": output.get("hq_city"),
                "hq_state": output.get("hq_state"),
                "hq_country": output.get("hq_country"),
            },
        }
    return {"success": True, "data": {"hq_city": None, "hq_state": None, "hq_country": None}}


@web_app.post("/company/find-hq-location-by-name-and-domain")
async def find_company_hq_location_by_name_and_domain(payload: CompanyNameAndDomainRequest) -> dict[str, Any]:
    company_name = payload.company_name.strip()
    company_domain = payload.company_domain.strip()
    if not company_name or not company_domain:
        return {"success": False, "error": "company_name and company_domain are required"}

    try:
        output = await run_parallel_task(
            task_spec=task_spec_find_company_hq_location_by_name_and_domain,
            input_data={"company_name": company_name, "company_domain": company_domain},
            processor="lite",
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    if isinstance(output, dict):
        return {
            "success": True,
            "data": {
                "hq_city": output.get("hq_city"),
                "hq_state": output.get("hq_state"),
                "hq_country": output.get("hq_country"),
            },
        }
    return {"success": True, "data": {"hq_city": None, "hq_state": None, "hq_country": None}}


@web_app.post("/company/find-employee-range-by-name-domain-and-linkedin-url")
async def find_company_employee_range_by_name_domain_and_linkedin_url(
    payload: CompanyNameDomainAndLinkedInUrlRequest,
) -> dict[str, Any]:
    company_name = payload.company_name.strip()
    company_domain = payload.company_domain.strip()
    company_linkedin_url = payload.company_linkedin_url.strip()
    if not company_name or not company_domain or not company_linkedin_url:
        return {"success": False, "error": "company_name, company_domain, and company_linkedin_url are required"}

    try:
        output = await run_parallel_task(
            task_spec=task_spec_find_company_employee_range_by_name_domain_and_linkedin_url,
            input_data={
                "company_name": company_name,
                "company_domain": company_domain,
                "company_linkedin_url": company_linkedin_url,
            },
            processor="base",
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    employee_range = output.get("employee_range") if isinstance(output, dict) else None
    return {"success": True, "data": {"employee_range": employee_range}}


# -----------------------------------------------------------------------------
# Person Endpoints
# -----------------------------------------------------------------------------


@web_app.post("/person/find-linkedin-url-by-full-name-company-name-and-company-domain")
async def find_person_linkedin_url_by_full_name_company_name_and_company_domain(
    payload: FullNameCompanyNameAndDomainRequest,
) -> dict[str, Any]:
    full_name = payload.full_name.strip()
    company_name = payload.company_name.strip()
    company_domain = payload.company_domain.strip()
    if not full_name or not company_name or not company_domain:
        return {"success": False, "error": "full_name, company_name, and company_domain are required"}

    try:
        output = await run_parallel_task(
            task_spec=task_spec_find_person_linkedin_url_by_full_name_company_name_and_company_domain,
            input_data={"full_name": full_name, "company_name": company_name, "company_domain": company_domain},
            processor="base",
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    linkedin_url = output.get("linkedin_url") if isinstance(output, dict) else None
    return {"success": True, "data": {"linkedin_url": linkedin_url}}


@web_app.post("/person/find-linkedin-url-by-name-and-company")
async def find_person_linkedin_url_by_name_and_company(
    payload: PersonNameAndCompanyRequest,
) -> dict[str, Any]:
    person_name = payload.person_name.strip()
    company_name = payload.company_name.strip()
    if not person_name or not company_name:
        return {"success": False, "error": "person_name and company_name are required"}

    try:
        output = await run_parallel_task(
            task_spec=task_spec_find_person_linkedin_url_by_name_and_company,
            input_data={"person_name": person_name, "company_name": company_name},
            processor="base",
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    linkedin_url = output.get("linkedin_url") if isinstance(output, dict) else None
    return {"success": True, "data": {"linkedin_url": linkedin_url}}


@web_app.post("/person/find-work-email-by-full-name-company-name-company-domain-and-linkedin-url")
async def find_person_work_email_by_full_name_company_name_company_domain_and_linkedin_url(
    payload: PersonFullNameCompanyNameDomainAndLinkedInUrlRequest,
) -> dict[str, Any]:
    person_full_name = payload.person_full_name.strip()
    company_name = payload.company_name.strip()
    company_domain = payload.company_domain.strip()
    person_linkedin_url = payload.person_linkedin_url.strip()
    if not person_full_name or not company_name or not company_domain or not person_linkedin_url:
        return {"success": False, "error": "person_full_name, company_name, company_domain, and person_linkedin_url are required"}

    try:
        output = await run_parallel_task(
            task_spec=task_spec_find_person_work_email_by_full_name_company_name_company_domain_and_linkedin_url,
            input_data={
                "person_full_name": person_full_name,
                "company_name": company_name,
                "company_domain": company_domain,
                "person_linkedin_url": person_linkedin_url,
            },
            processor="pro",
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    work_email = output.get("work_email") if isinstance(output, dict) else None
    return {"success": True, "data": {"work_email": work_email}}


@web_app.post("/person/find-work-email-by-full-name-company-name-and-company-domain")
async def find_work_email_by_full_name_company_name_and_company_domain(
    payload: FullNameCompanyNameAndDomainRequest,
) -> dict[str, Any]:
    full_name = payload.full_name.strip()
    company_name = payload.company_name.strip()
    company_domain = payload.company_domain.strip()
    if not full_name or not company_name or not company_domain:
        return {"success": False, "error": "full_name, company_name, and company_domain are required"}

    try:
        output = await run_parallel_task(
            task_spec=task_spec_find_work_email_by_full_name_company_name_and_company_domain,
            input_data={"full_name": full_name, "company_name": company_name, "company_domain": company_domain},
            processor="base",
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    work_email = output.get("work_email") if isinstance(output, dict) else None
    return {"success": True, "data": {"work_email": work_email}}


@web_app.post("/person/find-email-by-name-and-company")
async def find_person_email_by_name_and_company(
    payload: PersonFullNameAndCompanyRequest,
) -> dict[str, Any]:
    person_full_name = payload.person_full_name.strip()
    company_name = payload.company_name.strip()
    if not person_full_name or not company_name:
        return {"success": False, "error": "person_full_name and company_name are required"}

    try:
        output = await run_parallel_task(
            task_spec=task_spec_find_person_email_by_name_and_company,
            input_data={"person_full_name": person_full_name, "company_name": company_name},
            processor="base",
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    person_email = output.get("person_email") if isinstance(output, dict) else None
    return {"success": True, "data": {"person_email": person_email}}


@web_app.post("/person/find-email-and-linkedin-url-by-full-name-and-company-name")
async def find_person_email_and_linkedin_url_by_full_name_and_company_name(
    payload: FullNameAndCompanyNameRequest,
) -> dict[str, Any]:
    full_name = payload.full_name.strip()
    company_name = payload.company_name.strip()
    if not full_name or not company_name:
        return {"success": False, "error": "full_name and company_name are required"}

    try:
        output = await run_parallel_task(
            task_spec=task_spec_find_person_email_and_linkedin_url_by_full_name_and_company_name,
            input_data={"full_name": full_name, "company_name": company_name},
            processor="base",
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    if isinstance(output, dict):
        return {
            "success": True,
            "data": {
                "work_mail": output.get("work_mail"),
                "linkedin_url": output.get("linkedin_url"),
            },
        }
    return {"success": True, "data": {"work_mail": None, "linkedin_url": None}}


@web_app.post("/person/find-work-email-and-linkedin-url-by-full-name-company-name-and-company-domain")
async def find_person_work_email_and_linkedin_url_by_full_name_company_name_and_company_domain(
    payload: FullNameCompanyNameAndDomainRequest,
) -> dict[str, Any]:
    full_name = payload.full_name.strip()
    company_name = payload.company_name.strip()
    company_domain = payload.company_domain.strip()
    if not full_name or not company_name or not company_domain:
        return {"success": False, "error": "full_name, company_name, and company_domain are required"}

    try:
        output = await run_parallel_task(
            task_spec=task_spec_find_person_work_email_and_linkedin_url_by_full_name_company_name_and_company_domain,
            input_data={"full_name": full_name, "company_name": company_name, "company_domain": company_domain},
            processor="base",
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    if isinstance(output, dict):
        return {
            "success": True,
            "data": {
                "work_email": output.get("work_email"),
                "linkedin_url": output.get("linkedin_url"),
            },
        }
    return {"success": True, "data": {"work_email": None, "linkedin_url": None}}


@web_app.post("/person/find-location-by-full-name-and-linkedin-url")
async def find_person_location_by_full_name_and_linkedin_url(
    payload: PersonFullNameAndLinkedInUrlRequest,
) -> dict[str, Any]:
    person_full_name = payload.person_full_name.strip()
    person_linkedin_url = payload.person_linkedin_url.strip()
    if not person_full_name or not person_linkedin_url:
        return {"success": False, "error": "person_full_name and person_linkedin_url are required"}

    try:
        output = await run_parallel_task(
            task_spec=task_spec_find_person_location_by_full_name_and_linkedin_url,
            input_data={"person_full_name": person_full_name, "person_linkedin_url": person_linkedin_url},
            processor="base",
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    if isinstance(output, dict):
        return {
            "success": True,
            "data": {
                "city": output.get("city"),
                "state": output.get("state"),
                "country": output.get("country"),
            },
        }
    return {"success": True, "data": {"city": None, "state": None, "country": None}}


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=[
        modal.Secret.from_name("parallel-ai"),
        modal.Secret.from_name("internal-auth"),
    ],
)
@modal.asgi_app()
def fastapi_app() -> FastAPI:
    return web_app
