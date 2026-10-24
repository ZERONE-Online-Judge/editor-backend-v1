"""Keep execution categories stable across old and new saved jobs."""
from typing import Literal
from sqlalchemy import and_, func
from .db import Job

JobCategory = Literal['all', 'prepare', 'prepare-full', 'prepare-inputs', 'prepare-validate', 'prepare-answers',
                      'verify', 'validator-tests', 'checker-tests', 'stress', 'package', 'render']


def job_category(job):
    if job.kind != 'prepare':
        return job.kind
    mode = job.options.get('prepare_mode') or job.result.get('prepare_mode') or 'full'
    return 'prepare-' + mode if mode in {'full', 'inputs', 'validate', 'answers'} else 'prepare'


def category_condition(category):
    if category.startswith('prepare-'):
        mode = func.coalesce(Job.options['prepare_mode'].as_string(), Job.result['prepare_mode'].as_string(), 'full')
        return and_(Job.kind == 'prepare', mode == category.removeprefix('prepare-'))
    return Job.kind == category
