import json
import os
import re

import boto3
import botocore

from runtask_utils import generate_runtask_result
from tools.get_ami_releases import GetECSAmisReleases
from utils import logger, stream_messages, tool_config

# Initialize model_id and region
model_id = os.environ.get("BEDROCK_LLM_MODEL")
guardrail_id = os.environ.get("BEDROCK_GUARDRAIL_ID", None)
guardrail_version = os.environ.get("BEDROCK_GUARDRAIL_VERSION", None)

# Config to avoid timeouts when using long prompts
config = botocore.config.Config(
    read_timeout=1800, connect_timeout=1800, retries={"max_attempts": 0}
)

session = boto3.Session()
bedrock_client = session.client(
    service_name="bedrock-runtime", config=config
)

# Input is the terraform plan JSON
def eval(tf_plan_json):

    #####################################################################
    ##### First, do generic evaluation of the Terraform plan output #####
    #####################################################################

    logger.info("##### Evaluating Terraform plan output #####")
    prompt = """
    List the resources that will be created, modified or deleted in the following terraform plan using the following rules:
    1. Think step by step using the "thinking" json field
    2. For AMI changes, include the old and new AMI ID
    3. Use the following schema. Skip the preamble:
    <schema>
    {
        "$id": "https://example.com/arrays.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "thinking": {
                "type": "string",
                "description": "Think step by step"
            },
            "resources": {
                "type": "string",
                "description": "A list of resources that will be created, modified or deleted"
            }
        }
    }
    </schema>
    Here is an example of the output:
        <example>
        {
        "thinking": "To list the resources that will be created, modified or deleted, I will go through the terraform plan and look for the 'actions' field in each resource change. If the actions include 'create', 'update', or 'delete', I will add that resource to the list. For AMI changes, I will include the old and new AMI ID.",
        "resources": "The following resources will be modified: RESOURCES"
        }
        </example>
    Now, list the resources that will be created, modified or deleted in the following terraform plan"""

    prompt += f"""
    <terraform_plan>
    {tf_plan_json["resource_changes"]}
    </terraform_plan>
    """

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "text": prompt,
                }
            ],
        }
    ]

    system_text = "You are an assistant that helps reading infrastructure changes from JSON objects generated by terraform"

    stop_reason, analysis_response = stream_messages(
        bedrock_client, model_id, messages, system_text
    )

    logger.debug("Analysis response: {}".format(analysis_response))

    analysis_response_text= clean_response(analysis_response["content"][0]["text"])["resources"]

    logger.debug("Analysis response Text: {}".format(analysis_response_text))

    #####################################################################
    ######## Secondly, evaluate AMIs per analysis                ########
    #####################################################################
    logger.info("##### Evaluating AMI information #####")
    prompt = """
        Find additional details of infrastructure changes using the following rules
        1. For Amazon machine image (AMI or image_id) modifications, compare the old AMI information against the new AMI, including linux kernel, docker and ecs agent using the get_ami_releases function.
        2. Think step by step using "thinking" tags field
        3. Use the following schema. Skip the preamble:
        <output>
        <thinking>
        </thinking>
        <result>
            ## Current AMI ID
                * AMI name:
                * OS Architecture:
                * OS Name:
                * kernel:
                * docker version:
                * ECS agent:

            ## New AMI ID
                * AMI name:
                * kernel:
                * OS Architecture:
                * OS Name:
                * docker version:
                * ECS agent:
        </result>
        <output>
        Now, given the following analysis, compare any old with new AMIs:
        """

    prompt += f"""
        <analysis>{analysis_response_text}</analysis>
        """

    messages = [{"role": "user", "content": [{"text": prompt}]}]

    stop_reason, response = stream_messages(
        bedrock_client=bedrock_client,
        model_id=model_id,
        messages=messages,
        system_text=system_text,
        tool_config=tool_config,
    )

    # Add response to message history
    messages.append(response)

    # Check if there is an invoke function request from Claude
    while stop_reason == "tool_use":
        for content in response["content"]:
            if "toolUse" in content:
                tool = content["toolUse"]

                if tool["name"] == "GetECSAmisReleases":

                    release_details = GetECSAmisReleases().execute(
                        tool["input"]["image_ids"]
                    )
                    release_details_info = release_details if release_details else "No release notes were found the ami."

                    tool_result = {
                        "toolUseId": tool["toolUseId"],
                        "content": [{"json": {"release_detail": release_details_info}}],
                    }

                    tool_result_message = {
                        "role": "user",
                        "content": [{"toolResult": tool_result}],
                    }
                    # Add the result info to message array
                    messages.append(tool_result_message)

        # Send the messages, including the tool result, to the model.
        stop_reason, response = stream_messages(
            bedrock_client=bedrock_client,
            model_id=model_id,
            messages=messages,
            system_text=system_text,
            tool_config=tool_config,
            stop_sequences=["</result>"],
        )

        # Add response to message history
        messages.append(response)

    result = response["content"][0]["text"]

    #####################################################################
    ######### Third, generate short summary                     #########
    #####################################################################

    logger.info("##### Generating short summary #####")
    prompt = f"""
        Can you provide a short summary with maximum of 150 characters of the infrastructure changes?

        <terraform_plan>
        {tf_plan_json["resource_changes"]}
        </terraform_plan>
        """
    message_desc = [{"role": "user", "content": [{"text": prompt}]}]
    stop_reason, response = stream_messages(
        bedrock_client=bedrock_client,
        model_id=model_id,
        messages=message_desc,
        system_text=system_text,
        tool_config=tool_config,
        stop_sequences=["</result>"],
    )
    description = response["content"][0]["text"]

    logger.info("##### Report #####")
    logger.info("Analysis : {}".format(analysis_response_text))
    logger.info("AMI summary: {}".format(result))
    logger.info("Terraform plan summary: {}".format(description))

    results = []

    guardrail_status, guardrail_response = guardrail_inspection(str(description))
    if guardrail_status:
        results.append(generate_runtask_result(outcome_id="Plan-Summary", description="Summary of Terraform plan", result=description[:700]))
    else:
        results.append(generate_runtask_result(outcome_id="Plan-Summary", description="Summary of Terraform plan", result="Output omitted due to : {}".format(guardrail_response)))
        description = "Bedrock guardrail triggered : {}".format(guardrail_response)

    guardrail_status, guardrail_response = guardrail_inspection(str(result))
    if guardrail_status:
        results.append(generate_runtask_result(outcome_id="AMI-Summary", description="Summary of AMI changes", result=result[:700]))
    else:
        results.append(generate_runtask_result(outcome_id="AMI-Summary", description="Summary of AMI changes", result="Output omitted due to : {}".format(guardrail_response)))

    return description, results

def guardrail_inspection(input_text, input_mode = 'OUTPUT'):

    #####################################################################
    ##### Inspect input / output against Bedrock Guardrail          #####
    #####################################################################

    if guardrail_id and guardrail_version:
        logger.info("##### Scanning Terraform plan output with Amazon Bedrock Guardrail #####")

        response = bedrock_client.apply_guardrail(
            guardrailIdentifier=guardrail_id,
            guardrailVersion=guardrail_version,
            source=input_mode,
            content=[
                {
                    'text': {
                        'text': input_text,
                    }
                },
            ]
        )

        logger.debug("Guardrail inspection result : {}".format(json.dumps(response)))

        if response["action"] in ["GUARDRAIL_INTERVENED"]:
            logger.info("Guardrail action : {}".format(response["action"]))
            logger.info("Guardrail output : {}".format(response["outputs"]))
            logger.debug("Guardrail assessments : {}".format(response["assessments"]))
            return False, response["outputs"][0]["text"]

        elif response["action"] in ["NONE"]:
            logger.info("No Guardrail action required")
            return True, "No Guardrail action required"

    else:
        return True, "Guardrail inspection skipped"
    
def clean_response(json_str):
    # Remove any tags in the format <tag> or </tag>
    cleaned_str = re.sub(r'<\/?[\w\s]+>', '', json_str)
    last_brace_index = cleaned_str.rfind('}')
    cleaned_str = cleaned_str[:last_brace_index + 1]
    return json.loads(cleaned_str)