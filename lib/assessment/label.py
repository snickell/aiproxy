import os
import json
import re
import csv
import time
import requests
import logging

from typing import List, Dict, Any
from lib.assessment.config import VALID_LABELS

from io import StringIO

class InvalidResponseError(Exception):
    pass

class Label:
    def __init__(self):
        pass

    # This will take a rubric and student code and it will perform static checks on the code.
    #
    # For instance, it will determine a blank project should receive a No Evidence score for
    # all items in the rubric.
    def statically_label_student_work(self, rubric, student_code, student_id, examples=[]):
        rubric_key_concepts = list(set(row['Key Concept'] for row in csv.DictReader(rubric.splitlines())))

        if student_code.strip() == "":
            # Blank code should return No Evidence
            return {
                'metadata': {
                    'agent': 'static',
                },
                'data': list(
                    map(
                        lambda key_concept: {
                            "Label": "No Evidence",
                            "Key Concept": key_concept,
                            "Observations": "The program is empty.",
                            "Reason": "The program is empty.",
                        },
                        rubric_key_concepts
                    )
                )
            }

        # We can't assess this statically
        return None

    def ai_label_student_work(self, prompt, rubric, student_code, student_id, examples=[], num_responses=0, temperature=0.0, llm_model=""):
        # Determine the OpenAI URL and headers
        api_url = 'https://api.openai.com/v1/chat/completions'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f"Bearer {os.getenv('OPENAI_API_KEY')}"
        }

        # Compute the input we are giving to OpenAI
        messages = self.compute_messages(prompt, rubric, student_code, examples=examples)
        data = {
            'model': llm_model,
            'temperature': temperature,
            'messages': messages,
            'n': num_responses,
        }

        # Post to the AI service
        response = requests.post(api_url, headers=headers, json=data, timeout=120)

        if response.status_code != 200:
            logging.error(f"{student_id} Error calling the API: {response.status_code}")
            logging.info(f"{student_id} Response body: {response.text}")
            return None

        info = response.json()

        response_data = self.response_data_from_choices(info, rubric, student_id)

        return {
            'metadata': {
                'agent': 'openai',
                'usage': info['usage'],
                'request': data,
            },
            'data': response_data,
        }

    def label_student_work(self, prompt, rubric, student_code, student_id, examples=[], use_cached=False, write_cached=False, num_responses=0, temperature=0.0, llm_model="", remove_comments=False, cache_prefix=""):
        if use_cached and os.path.exists(os.path.join(cache_prefix, f"cached_responses/{student_id}.json")):
            with open(os.path.join(cache_prefix, f"cached_responses/{student_id}.json"), 'r') as f:
                return json.load(f)

        # We will record the time it takes to perform the assessment
        start_time = time.time()

        # Sanitize student code
        student_code = self.sanitize_code(student_code, remove_comments=remove_comments)

        # Try static analysis options (before invoking AI)
        result = self.statically_label_student_work(rubric, student_code, student_id, examples=examples)

        # If it gives back a response, right now assume it is complete and do not perform an AI analysis
        # We may want to, in the future, gauge how many of the concepts it has labeled and let AI fill in the blanks
        # Right now, however, only if there is no result, we try the AI for assessment
        if result is None:
            try:
                result = self.ai_label_student_work(prompt, rubric, student_code, student_id, examples=examples, num_responses=num_responses, temperature=temperature, llm_model=llm_model)
            except requests.exceptions.ReadTimeout:
                logging.error(f"{student_id} request timed out in {(time.time() - start_time):.0f} seconds.")
                result = None

        # No assessment was possible
        if result is None:
            raise Exception("AI assessment failed.")

        elapsed = time.time() - start_time
        tokens = result.get('metadata', {}).get('usage', {}).get('total_tokens', 0)
        logging.info(f"{student_id} request succeeded in {elapsed:.0f} seconds. {tokens} tokens used.")

        # Craft the response dictionary
        response = {
            'metadata': {
                'time': elapsed,
                'student_id': student_id,
            },
            'data': result.get('data', []),
        }
        response['metadata'].update(result.get('metadata', {})),

        # only write to cache if the response is valid
        if write_cached and result:
            with open(os.path.join(cache_prefix, f"cached_responses/{student_id}.json"), 'w+') as f:
                json.dump(response, f, indent=4)

        return response

    def remove_js_comments(self, code):
        # This regex pattern captures three groups:
        # 1) Single or double quoted strings
        # 2) Multi-line comments
        # 3) Single-line comments
        pattern = r'(".*?[^\\]"|\'.*?[^\\]\'|/\*.*?\*/|//.*?$)'

        def replacer(match):
            # If the match is a string, return it unchanged
            if match.group(0).startswith(("'", '"')):
                return match.group(0)

            # Otherwise, it's a comment, so remove it
            return ''

        return re.sub(pattern, replacer, code, flags=re.DOTALL | re.MULTILINE)

    def sanitize_code(self, student_code, remove_comments=False):
        # Remove comments
        if remove_comments:
            student_code = self.remove_js_comments(student_code)

        return student_code

    def response_data_from_choices(self, info, rubric, student_id):
        max_index = len(info['choices']) - 1
        response_data_choices = []
        for index, choice in enumerate(info['choices']):
            # If all choices result in an InvalidResponseError, reraise the last one.
            reraise = len(response_data_choices) == 0 and index == max_index

            if choice['message']['content']:
                response_data = self.get_response_data_if_valid(choice['message']['content'], rubric, student_id, choice_index=index, reraise=reraise)
                if response_data:
                    response_data_choices.append(response_data)

        if len(response_data_choices) == 0:
            raise InvalidResponseError("No valid responses. An InvalidResponseError should have been raised earlier.")
        elif len(response_data_choices) == 1:
            response_data = response_data_choices[0]
        else:
            response_data = self.get_consensus_response(response_data_choices, student_id)
        return response_data

    def compute_messages(self, prompt, rubric, student_code, examples=[]):
        messages = [
            {'role': 'system', 'content': f"{prompt}\n\nRubric:\n{rubric}"}
        ]
        for example_js, example_rubric in examples:
            messages.append({'role': 'user', 'content': example_js})
            messages.append({'role': 'assistant', 'content': example_rubric})
        messages.append({'role': 'user', 'content': student_code})
        return messages

    def get_response_data_if_valid(self, response_text, rubric, student_id, choice_index=None, reraise=False):
        choice_text = f"Choice {choice_index}: " if choice_index is not None else ''
        if not response_text:
            logging.error(f"{student_id} {choice_text} Invalid response: empty response")
            return None
        text = response_text.strip()

        response_data = self.parse_non_json_response(text)

        try:
            self._sanitize_server_response(response_data)
            self._validate_server_response(response_data, rubric)
            return [row for row in response_data]
        except InvalidResponseError as e:
            logging.error(f"{student_id} {choice_text} Invalid response: {str(e)}\n{response_text}")
            if reraise:
                raise e
            return None

    # parse response data in tsv, csv or markdown format.
    def parse_non_json_response(self, text):
        # Remove anything up to the first column name
        if "\nKey Concept" in text:
            index = text.index("\nKey Concept")
            text = text[index:].strip()
        # Replace escaped tabs
        if '\\t' in text:
            text = text.replace("\\t", "\t")
        # Replace double tabs... ugh
        text = text.replace("\t\t", "\t")
        # If there is a tab, it is probably TSV
        if '\t' not in text:
            if ' | ' in text:
                # Ok, sometimes it does markdown sequence... which means it does '|'
                # as a delimiter and has lines with '---' in them
                lines = text.split('\n')
                lines = list(filter(lambda x: "---" not in x, lines))
                text = "\n".join(lines)
                logging.info("response was markdown and not tsv, delimiting by '|'")

                response_data = list(csv.DictReader(StringIO(text), delimiter='|'))
            else:
                # Let's assume it is CSV
                logging.info("response had no tabs so is not tsv, delimiting by ','")
                response_data = list(csv.DictReader(StringIO(text), delimiter=','))
        else:
            # Let's assume it is TSV
            response_data = list(csv.DictReader(StringIO(text), delimiter='\t'))
        return response_data

    def _sanitize_server_response(self, response_data):
        # Strip whitespace and quotes from fields
        for row in response_data:
            for key in list(row.keys()):
                if isinstance(row[key], str):
                    row[key] = row[key].strip().strip('"')

                if isinstance(key, str):
                    if key.strip() != key:
                        row[key.strip()] = row[key]
                        del row[key]

        # Remove rows that don't start with reasonable things
        for row in response_data:
            if "Key Concept" in row:
                if not row["Key Concept"][0:1].isalnum():
                    response_data.remove(row)

        for row in response_data:
            if "Grade" in row.keys():
                row['Label'] = row['Grade']
                del row['Grade']

    def _validate_server_response(self, response_data, rubric):
        expected_columns = ["Key Concept", "Observations", "Label", "Reason"]

        rubric_key_concepts = list(set(row['Key Concept'] for row in csv.DictReader(rubric.splitlines())))

        if not all((set(row.keys()) & set(expected_columns)) == set(expected_columns) for row in response_data):
            for row in response_data:
                unexpected_columns = set(row.keys()) - set(expected_columns)
                missing_columns = set(expected_columns) - set(row.keys())
                raise InvalidResponseError(f'incorrect column names. unexpected: {unexpected_columns} missing: {missing_columns}')

        key_concepts_from_response = list(set(row["Key Concept"] for row in response_data))
        if sorted(rubric_key_concepts) != sorted(key_concepts_from_response):
            unexpected_concepts = set(key_concepts_from_response) - set(rubric_key_concepts)
            unexpected_concepts = None if len(unexpected_concepts) == 0 else unexpected_concepts
            missing_concepts = set(rubric_key_concepts) - set(key_concepts_from_response)
            missing_concepts = None if len(missing_concepts) == 0 else missing_concepts
            raise InvalidResponseError(f'unexpected or missing key concept. unexpected: {unexpected_concepts} missing: {missing_concepts}')

        for row in response_data:
            if row['Label'] not in VALID_LABELS:
                raise InvalidResponseError(f"invalid label value: '{row['Label']}'")

    def get_consensus_response(self, choices, student_id):
        from collections import Counter

        key_concept_to_labels = {}
        for choice in choices:
            for row in choice:
                if row['Key Concept'] not in key_concept_to_labels:
                    key_concept_to_labels[row['Key Concept']] = []
                key_concept_to_labels[row['Key Concept']].append(row['Label'])

        key_concept_to_majority_label = {}
        for key_concept, labels in key_concept_to_labels.items():
            majority_label = Counter(labels).most_common(1)[0][0]
            key_concept_to_majority_label[key_concept] = majority_label
            if majority_label != labels[0]:
                logging.info(f"outvoted {student_id} Key Concept: {key_concept} first label: {labels[0]} majority label: {majority_label}")

        key_concept_to_observations = {}
        key_concept_to_reason = {}
        for choice in choices:
            for row in choice:
                key_concept = row['Key Concept']
                if key_concept_to_majority_label[key_concept] == row['Label']:
                    if key_concept not in key_concept_to_observations:
                        key_concept_to_observations[key_concept] = row['Observations']
                    key_concept_to_reason[key_concept] = row['Reason']

        return [{'Key Concept': key_concept, 'Observations': key_concept_to_observations[key_concept], 'Label': label, 'Reason': f"{self.get_consensus_votes(key_concept_to_labels[key_concept])}{key_concept_to_reason[key_concept]}"} for key_concept, label in key_concept_to_majority_label.items()]

    def get_consensus_votes(self, labels):
        # only display votes if there is a disagreement
        if len(set(labels)) == 1:
            return ""
        return f"<b>Votes: [{', '.join(labels)}]</b><br>"
