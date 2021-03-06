# -*- encoding: utf-8 -*-
#
# Copyright © 2018-2020 Mergify SAS
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import base64
import dataclasses
import functools
import itertools
import operator
import typing

import daiquiri
import voluptuous
import yaml

from mergify_engine import actions
from mergify_engine import context
from mergify_engine import utils
from mergify_engine.clients import http
from mergify_engine.rules import filter
from mergify_engine.rules import types


LOG = daiquiri.getLogger(__name__)


def RuleCondition(value: str) -> filter.Filter:
    try:
        return filter.Filter.parse(value)
    except filter.parser.pyparsing.ParseException as e:
        raise voluptuous.Invalid(
            message="Invalid condition '%s'. %s" % (value, str(e)), error_message=str(e)
        )
    except filter.InvalidQuery as e:
        raise voluptuous.Invalid(
            message="Invalid condition '%s'. %s" % (value, str(e)), error_message=str(e)
        )


RuleConditions = typing.NewType("RuleConditions", typing.List[filter.Filter])
RuleMissingConditions = typing.NewType(
    "RuleMissingConditions", typing.List[filter.Filter]
)


@dataclasses.dataclass
class Rule:
    name: str
    conditions: RuleConditions
    actions: typing.Dict[str, actions.Action]
    hidden: bool = False

    class T_from_dict_required(typing.TypedDict):
        name: str
        conditions: RuleConditions
        actions: typing.Dict[str, actions.Action]

    class T_from_dict(T_from_dict_required, total=False):
        hidden: bool

    @classmethod
    def from_dict(cls, d: T_from_dict) -> "Rule":
        return cls(**d)


@dataclasses.dataclass
class EvaluatedRule:
    name: str
    conditions: RuleConditions
    missing_conditions: RuleMissingConditions
    actions: typing.Dict[str, actions.Action]
    hidden: bool = False

    @classmethod
    def from_rule(
        cls, rule: "Rule", missing_conditions: RuleMissingConditions
    ) -> "EvaluatedRule":
        return cls(
            rule.name,
            rule.conditions,
            missing_conditions,
            rule.actions,
            rule.hidden,
        )


@dataclasses.dataclass
class RulesEvaluator:
    """A rules that matches a pull request."""

    # Fixed base attributes that are not considered when looking for the
    # next matching rules.
    BASE_ATTRIBUTES = (
        "head",
        "base",
        "author",
        "merged_by",
    )
    TEAM_ATTRIBUTES = (
        "author",
        "merged_by",
        "approved-reviews-by",
        "dismissed-reviews-by",
        "commented-reviews-by",
    )

    # The list of pull request rules to match against.
    rules: typing.List

    # The context to test.
    context: context.Context

    rule_class: object

    # The rules with BASE_ATTRIBUTES not matched are saved in ignored_rules
    hide_rule: bool

    # The rules matching the pull request.
    matching_rules: typing.List = dataclasses.field(init=False, default_factory=list)

    # The rules not matching the pull request.
    ignored_rules: typing.List = dataclasses.field(init=False, default_factory=list)

    def __post_init__(self):
        for rule in self.rules:
            ignore_rules = False
            next_conditions_to_validate = []
            for condition in rule.conditions:
                for attrib in self.TEAM_ATTRIBUTES:
                    condition.set_value_expanders(
                        attrib,
                        self.context.resolve_teams,
                    )

                name = condition.get_attribute_name()
                value = getattr(self.context.pull_request, name)
                if not condition(**{name: value}):
                    next_conditions_to_validate.append(condition)
                    if condition.attribute_name in self.BASE_ATTRIBUTES:
                        ignore_rules = True

            if ignore_rules and self.hide_rule:
                self.ignored_rules.append(
                    self.rule_class.from_rule(rule, next_conditions_to_validate)
                )
            else:
                self.matching_rules.append(
                    self.rule_class.from_rule(rule, next_conditions_to_validate)
                )


@dataclasses.dataclass
class PullRequestRules:
    rules: typing.List[Rule]

    def __post_init__(self):
        # Make sure each rule has a unique name
        sorted_rules = sorted(self.rules, key=operator.attrgetter("name"))
        grouped_rules = itertools.groupby(sorted_rules, operator.attrgetter("name"))
        for name, sub_rules in grouped_rules:
            sub_rules = list(sub_rules)
            if len(sub_rules) == 1:
                continue
            for n, rule in enumerate(sub_rules):
                rule.name += " #%d" % (n + 1)

    def __iter__(self):
        return iter(self.rules)

    def get_pull_request_rule(self, pull_request: context.Context) -> RulesEvaluator:
        return RulesEvaluator(self.rules, pull_request, EvaluatedRule, True)


class YAMLInvalid(voluptuous.Invalid):
    def __str__(self):
        return f"{self.msg} at {self.path}"

    def get_annotations(self, path):
        if self.path:
            error_path = self.path[0]
            return [
                {
                    "path": path,
                    "start_line": error_path.line,
                    "end_line": error_path.line,
                    "start_column": error_path.column,
                    "end_column": error_path.column,
                    "annotation_level": "failure",
                    "message": self.error_message,
                    "title": self.msg,
                },
            ]
        return []


def YAML(v):
    try:
        return yaml.safe_load(v)
    except yaml.YAMLError as e:
        error_message = str(e)
        path = (
            [types.LineColumnPath(e.problem_mark.line + 1, e.problem_mark.column + 1)]
            if hasattr(e, "problem_mark")
            else None
        )
        raise YAMLInvalid(
            message="Invalid YAML", error_message=error_message, path=path
        )
    return v


PullRequestRulesSchema = voluptuous.All(
    [
        voluptuous.All(
            {
                voluptuous.Required("name"): str,
                voluptuous.Required("hidden", default=False): bool,
                voluptuous.Required("conditions"): [
                    voluptuous.All(str, voluptuous.Coerce(RuleCondition))
                ],
                voluptuous.Required("actions"): actions.get_action_schemas(),
            },
            voluptuous.Coerce(Rule.from_dict),
        ),
    ],
    voluptuous.Length(min=1),
    voluptuous.Coerce(PullRequestRules),
)


UserConfigurationSchema = voluptuous.Schema(
    voluptuous.And(
        voluptuous.Coerce(YAML),
        {
            voluptuous.Required("pull_request_rules"): PullRequestRulesSchema,
        },
    )
)


class NoRules(Exception):
    def __init__(self):
        super().__init__("Mergify configuration file is missing")


@dataclasses.dataclass
class InvalidRules(Exception):
    error: voluptuous.Invalid
    filename: str

    @staticmethod
    def _format_path_item(path_item):
        if isinstance(path_item, int):
            return f"item {path_item}"
        return str(path_item)

    def _format_error(self, error):
        msg = str(error.msg)

        if error.error_type:
            msg += f" for {error.error_type}"

        if error.path:
            path = " → ".join(map(self._format_path_item, error.path))
            msg += f" @ {path}"
        # Only include the error message if it has been provided
        # voluptuous set it to the `message` otherwise
        if error.error_message != error.msg:
            msg += f"\n```\n{error.error_message}\n```"
        return msg

    @property
    def errors(self):
        if isinstance(self.error, voluptuous.MultipleInvalid):
            return self.error.errors
        return [self.error]

    def __str__(self):
        if len(self.errors) >= 2:
            return "* " + "\n* ".join(sorted(map(self._format_error, self.errors)))
        return self._format_error(self.errors[0])

    def get_annotations(self, path):
        return functools.reduce(
            operator.add,
            (
                error.get_annotations(path)
                for error in self.errors
                if hasattr(error, "get_annotations")
            ),
            [],
        )


MERGIFY_CONFIG_FILENAMES = [
    ".mergify.yml",
    ".mergify/config.yml",
    ".github/mergify.yml",
]


def get_config_location_cache_key(owner, repo):
    return f"config-location~{owner}~{repo}"


def get_mergify_config_content(client, repo, ref=None):
    """Get the Mergify configuration file content.

    :return: The filename and its content.
    """

    config_location_cache = get_config_location_cache_key(client.auth.owner, repo)

    kwargs = {}
    if ref:
        kwargs["ref"] = ref
        cached_filename = None
    else:
        with utils.get_redis_for_cache() as redis:
            cached_filename = redis.get(config_location_cache)

    filenames = MERGIFY_CONFIG_FILENAMES.copy()
    if cached_filename:
        filenames.remove(cached_filename)
        filenames.insert(0, cached_filename)

    for filename in filenames:
        try:
            content = client.item(
                f"/repos/{client.auth.owner}/{repo}/contents/{filename}", **kwargs
            )["content"]
        except http.HTTPNotFound:
            continue
        if ref is None and filename != cached_filename:
            with utils.get_redis_for_cache() as redis:
                redis.set(config_location_cache, filename, ex=60 * 60 * 24 * 31)

        return filename, base64.b64decode(bytearray(content, "utf-8"))
    with utils.get_redis_for_cache() as redis:
        redis.delete(config_location_cache)
    raise NoRules()


class MergifyConfig(typing.TypedDict):
    pull_request_rules: PullRequestRules


def get_mergify_config(client, repo, ref=None):
    filename, content = get_mergify_config_content(client, repo, ref)
    try:
        return filename, typing.cast(MergifyConfig, UserConfigurationSchema(content))
    except voluptuous.Invalid as e:
        raise InvalidRules(e, filename)
