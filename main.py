from src.client.naukri_client import NaukriLoginClient
from src.client.job_client import NaukriJobClient
from colorama import Fore, Style, init
import time

init(autoreset=True)


if __name__ == "__main__":
    client = NaukriLoginClient()
    client.login()

    jc = NaukriJobClient(client)

    print("Searching jobs...")
    jobs = jc.search_jobs(keyword="Node.js developer", location="Hyderabad", experience=2)

    if not jobs:
        print(f"{Fore.YELLOW}  No jobs found.{Style.RESET_ALL}")
    else:
        print(f"{Fore.GREEN}Found {len(jobs)} jobs{Style.RESET_ALL}")

        for job in jobs:
            print(f"\n{Fore.CYAN}{'-' * 50}{Style.RESET_ALL}")
            print(f"{Fore.WHITE}  Title   : {Fore.YELLOW}{job.title}")
            print(f"{Fore.WHITE}  Company : {Fore.YELLOW}{job.company}")
            print(f"{Fore.WHITE}  Job ID  : {Fore.YELLOW}{job.job_id}")
            print(f"{Fore.WHITE}  Tags    : {Fore.YELLOW}{job.tags}")

            mandatory = job.tags[:2] if job.tags else []
            optional = job.tags[2:] if len(job.tags) > 2 else []

            try:
                result = jc.apply_job(
                    job,
                    mandatory_skills=mandatory,
                    optional_skills=optional,
                    source="recommended",
                )

                job_result = (result.get("jobs") or [{}])[0]
                if job_result.get("questionnaire"):
                    print(f"{Fore.YELLOW}   Skipped - questionnaire required{Style.RESET_ALL}")
                    continue

                print(f"{Fore.GREEN}  Applied successfully{Style.RESET_ALL}")

            except Exception as e:
                print(f"{Fore.RED}   Failed: {e}{Style.RESET_ALL}")

            time.sleep(3)
