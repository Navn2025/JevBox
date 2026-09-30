import os
from dotenv import load_dotenv
load_dotenv()

from typesafe_sdk import TypeSafeClient,Choice, Noul, Score

client=TypeSafeClient(
api_key=os.environ["OPENROUTER_API_KEY"],
base_url="https://openrouter.ai/api"
)
ticket ="Hi, I've been trying to connect my Stripe account for 3 days and the integration keeps failing. I'm losing sales. Please help ASAP."
response = client.system_one(
    state=ticket,
    questions={
        "department":Choice(
            instructions="Which team should handle this",
            criteria={
                "Sales": "For issues related to sales and revenue",
                "Technical Support": "For technical issues and troubleshooting",
                "Billing": "For billing and payment issues",
                "General Inquiry": "For general questions or information"
            }
        ),
        "frustration":Score(
            instructions="How frustrated is the customer on a scale of 1 to 10?",
            criteria=[
                 "Not frustrated at all",
                 "Slightly frustrated",
                 "Somewhat frustrated",
                 "Moderately frustrated",
                 "Very frustrated",
                 "Extremely frustrated",
                 "On the verge of giving up",
                 "Frustrated and angry",
                 "Frustrated and upset",
                "Completely frustrated and desperate"
            ]
        ),
        "is_urgent":Noul(
            instructions="The message conveys urgency or time-sensitivity"
        )
    }

)

print(response.answers["department"].choice)  # "technical"
print(response.answers["frustration"].score)  # 1.0
print(response.answers["is_urgent"].noul)