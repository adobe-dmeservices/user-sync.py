import csv
import re

import mock
import pytest
import six
import yaml
from mock import MagicMock

from tests.util import compare_iter
from user_sync.connector.umapi import Commands
from user_sync.rules import RuleProcessor, AdobeGroup, UmapiTargetInfo
from user_sync.rules import UmapiConnectors


@pytest.fixture
def rule_processor():
    return RuleProcessor({})


@pytest.fixture()
def mock_umapi_connectors():
    def _mock_umapi_connectors(*args):
        return UmapiConnectors(
            MockUmapiConnector(),
            {a: MockUmapiConnector(name=a) for a in args})

    return _mock_umapi_connectors


@pytest.fixture()
def mock_umapi_info():
    def _mock_umapi_info(name=None, groups=[]):
        mock_umapi_info = UmapiTargetInfo(name)
        for g in groups:
            mock_umapi_info.add_mapped_group(g)
        return mock_umapi_info

    return _mock_umapi_info


class MockUmapiConnector(MagicMock):
    class MockActionManager:
        def get_statistics(self):
            return 10, 2

    def __init__(self, name='', options={}, *args, **kwargs):
        super(MockUmapiConnector, self).__init__(*args, **kwargs)
        self.name = 'umapi' + name
        self.trusted = False
        self.options = options
        self.action_manager = MockUmapiConnector.MockActionManager()
        self.commands_sent = None
        self.users = {}

    def send_commands(self, commands):
        self.commands_sent = commands

    def get_action_manager(self):
        return self.action_manager

    def iter_users(self):
        return self.users


def test_log_action_summary(rule_processor, log_stream, mock_umapi_connectors):
    connectors = mock_umapi_connectors('umapi-2', 'umapi-3')
    stream, rule_processor.logger = log_stream
    rule_processor.log_action_summary(connectors)
    expected = """---------------------------------- Action Summary ----------------------------------
                                Number of directory users read: 0
                  Number of directory users selected for input: 0
                                    Number of Adobe users read: 0
                   Number of Adobe users excluded from updates: 0
            Number of non-excluded Adobe users with no changes: 0
                               Number of new Adobe users added: 0
                        Number of matching Adobe users updated: 0
                           Number of Adobe user-groups created: 0
                    Number of Adobe users added to secondaries: 0
  Number of primary UMAPI actions sent (total, success, error): (10, 8, 2)
  Number of umapi-2 UMAPI actions sent (total, success, error): (10, 8, 2)
  Number of umapi-3 UMAPI actions sent (total, success, error): (10, 8, 2)
------------------------------------------------------------------------------------
"""
    assert expected == stream.getvalue()


def test_read_desired_user_groups_basic(rule_processor, mock_dir_user):
    rp = rule_processor
    mock_dir_user['groups'] = ['Group A', 'Group B']

    directory_connector = mock.MagicMock()
    directory_connector.load_users_and_groups.return_value = [mock_dir_user]
    mappings = {
        'Group A': [AdobeGroup.create('Console Group')]}
    rp.read_desired_user_groups(mappings, directory_connector)

    # Assert the security group and adobe group end up in the correct scope
    assert "Group A" in rp.after_mapping_hook_scope['source_groups']
    assert "Console Group" in rp.after_mapping_hook_scope['target_groups']

    # Assert the user group updated in umapi info
    user_key = rp.get_directory_user_key(mock_dir_user)
    assert ('console group' in rp.umapi_info_by_name[None].desired_groups_by_user_key[user_key])
    assert user_key in rp.filtered_directory_user_by_user_key


def test_after_mapping_hook(rule_processor, mock_dir_user):
    rp = rule_processor
    mock_dir_user['groups'] = ['Group A']
    directory_connector = mock.MagicMock()
    directory_connector.load_users_and_groups.return_value = [mock_dir_user]

    # testing after_mapping_hooks
    after_mapping_hook_text = """
first = source_attributes.get('givenName')
if first is not None: 
  target_groups.add('ext group 1')
target_groups.add('ext group 2')
"""

    AdobeGroup.create('existing_group')
    rp.options['after_mapping_hook'] = compile(
        after_mapping_hook_text, '<per-user after-mapping-hook>', 'exec')

    rp.read_desired_user_groups({}, directory_connector)
    assert "Group A" in rp.after_mapping_hook_scope['source_groups']
    assert "ext group 1" in rp.after_mapping_hook_scope['target_groups']
    assert "ext group 2" in rp.after_mapping_hook_scope['target_groups']


def test_additional_groups(rule_processor, mock_dir_user):
    rp = rule_processor
    mock_dir_user['member_groups'] = ['other_security_group', 'security_group', 'more_security_group']

    directory_connector = mock.MagicMock()
    directory_connector.load_users_and_groups.return_value = [mock_dir_user]
    user_key = rp.get_directory_user_key(mock_dir_user)

    rp.options['additional_groups'] = [
        {
            'source': re.compile('other(.+)'),
            'target': AdobeGroup.create('additional_user_group')},
        {
            'source': re.compile('security_group'),
            'target': AdobeGroup.create('additional(.+)')}
    ]

    rp.read_desired_user_groups({}, directory_connector)
    assert 'other_security_group' in rp.umapi_info_by_name[None].additional_group_map['additional_user_group']
    assert 'security_group' in rp.umapi_info_by_name[None].additional_group_map['additional(.+)']
    assert {'additional_user_group', 'additional(.+)'}.issubset(
        rp.umapi_info_by_name[None].desired_groups_by_user_key[user_key])


@mock.patch("user_sync.rules.RuleProcessor.update_umapi_users_for_connector")
def test_sync_umapi_users(update_umapi, rule_processor, mock_umapi_connectors, get_mock_user_list, mock_umapi_info):
    rule_processor.options['exclude_unmapped_users'] = False
    refine = lambda u: {k: set(u[k].pop('groups')) for k in u}
    groups = ['Group A', 'Group B']

    primary_users = refine(get_mock_user_list(5, start=0, groups=groups))
    secondary_users = refine(get_mock_user_list(5, start=6, groups=groups))
    tertiary_users = refine(get_mock_user_list(5, start=11, groups=groups))

    # Create 3 umapi connectors - 1 primary, 2 secondary
    secondary_umapi_name = 'umapi-2'
    third_umapi_name = 'umapi-3'
    umapi_connectors = mock_umapi_connectors(secondary_umapi_name, third_umapi_name)
    umapi_info = mock_umapi_info(secondary_umapi_name, groups)

    # Add the umapi infos + group for secondaries so they will not be skipped
    rule_processor.umapi_info_by_name[secondary_umapi_name] = umapi_info
    rule_processor.umapi_info_by_name[third_umapi_name] = umapi_info

    # Use a mock object here to collect the calls made for validation
    rule_processor.create_umapi_user = mock.MagicMock()
    update_umapi.side_effect = [primary_users, secondary_users, tertiary_users]
    rule_processor.sync_umapi_users(umapi_connectors)

    # Combine the secondary users
    secondary_users.update(tertiary_users)

    # Check that the users were correctly returned and sorted from update_umapi_users calls
    assert compare_iter(rule_processor.primary_users_created, primary_users.keys())
    assert compare_iter(rule_processor.secondary_users_created, secondary_users.keys())

    # Checks that create user was called for all of the users
    results = [c[1][0:2] for c in rule_processor.create_umapi_user.mock_calls]

    # Now combine all the users and check
    secondary_users.update(primary_users)
    actual = [(k, v) for k, v in six.iteritems(secondary_users)]
    assert compare_iter(results, actual)


def test_create_umapi_groups(rule_processor, mock_umapi_connectors, mock_umapi_info):
    secondary_umapi_name = 'umapi-2'
    uc = mock_umapi_connectors(secondary_umapi_name)
    sec_conn = uc.secondary_connectors[secondary_umapi_name]
    sec_conn.get_groups.return_value = {}
    uc.primary_connector.get_groups.return_value = [
        {
            'groupId': 94663221,
            'groupName': 'existing_group',
            'type': 'SYSADMIN_GROUP',
            'memberCount': 41},
        {
            'groupId': 94663220,
            'groupName': 'misc_group',
            'type': 'SYSADMIN_GROUP',
            'memberCount': 150
        }
    ]

    primary_info = mock_umapi_info(groups=['new_group', 'existing_group'])
    sec_info = mock_umapi_info(secondary_umapi_name, ['new_group_2', 'new_group_3'])
    rule_processor.umapi_info_by_name = {
        None: primary_info,
        sec_conn.name: sec_info}
    rule_processor.create_umapi_groups(uc)

    calls = [c[1] for c in uc.primary_connector.mock_calls]
    calls.extend([c[1] for c in sec_conn.mock_calls])
    calls = [c[0] for c in calls if c]
    assert compare_iter(calls, ['new_group', 'new_group_2', 'new_group_3'])


def test_process_strays(rule_processor, log_stream):
    rp = rule_processor
    rp.will_manage_strays = True
    rp.manage_strays = MagicMock()
    rp.stray_key_map = {
        None: {
            'federatedID,testuser2000@example.com,': set()
        }
    }

    def straysProcessed():
        called = rp.manage_strays.mock_calls != []
        rp.manage_strays = MagicMock()
        return called

    rp.options["max_adobe_only_users"] = 200
    rp.process_strays(None)
    assert straysProcessed()

    rp.options["max_adobe_only_users"] = 0
    rp.process_strays(None)
    assert not straysProcessed()

    rp.primary_user_count = 10
    rp.excluded_user_count = 1
    rp.options["max_adobe_only_users"] = "5%"
    rp.process_strays(None)
    assert not straysProcessed()

    rp.options["max_adobe_only_users"] = "20%"
    rp.process_strays(None)
    assert straysProcessed()


def test_create_umapi_commands_for_directory_user(rule_processor, mock_dir_user):
    rp = rule_processor
    user = mock_dir_user

    def get_commands(user, update_option='ignoreIfAlreadyExists'):
        attributes = rp.get_user_attributes(user)
        attributes['country'] = user['country']
        attributes['option'] = update_option
        commands = Commands(user['identity_type'], user['email'], user['username'], user['domain'])
        commands.add_user(attributes)
        return commands

    # simple case
    commands = get_commands(user)
    result = rp.create_umapi_commands_for_directory_user(user)
    assert vars(result) == vars(commands)

    # test do_update
    commands = get_commands(user, 'updateIfAlreadyExists')
    result = rp.create_umapi_commands_for_directory_user(user, do_update=True)
    assert vars(result) == vars(commands)

    # test username format
    user['username'] = 'nosymbol'
    commands = get_commands(user)
    result = rp.create_umapi_commands_for_directory_user(user)
    assert vars(result) == vars(commands)

    # test username format
    user['username'] = 'different@example.com'
    commands = get_commands(user)
    commands.update_user({
        "email": user['email'],
        "username": user['username']})
    commands.username = user['email']
    result = rp.create_umapi_commands_for_directory_user(user)
    assert vars(result) == vars(commands)

    # test console trusted
    user['username'] = 'different@example.com'
    commands = get_commands(user)
    commands.username = user['email']
    result = rp.create_umapi_commands_for_directory_user(user, console_trusted=True)
    assert vars(result) == vars(commands)

    # Default Country Code as None and Id Type as federatedID. Country as None in user
    rp.options['default_country_code'] = None
    user['country'] = None
    result = rp.create_umapi_commands_for_directory_user(user)
    assert result == None

    # Default Country Code as None with Id Type as enterpriseID. Country as None in user
    rp.options['default_country_code'] = None
    user['identity_type'] = 'enterpriseID'
    result = rp.create_umapi_commands_for_directory_user(user)
    user['country'] = 'UD'
    commands = get_commands(user)
    assert vars(result) == vars(commands)

    # Having Default Country Code with value 'US'. Country as None in user.
    rp.options['default_country_code'] = 'US'
    user['country'] = None
    result = rp.create_umapi_commands_for_directory_user(user)
    user['country'] = 'US'
    commands = get_commands(user)
    assert vars(result) == vars(commands)

    # Country as 'CA' in user
    user['country'] = 'CA'
    result = rp.create_umapi_commands_for_directory_user(user)
    commands = get_commands(user)
    assert vars(result) == vars(commands)


def test_create_umapi_user(rule_processor, mock_dir_user, mock_umapi_info):
    user = mock_dir_user
    rp = rule_processor

    key = rp.get_user_key(user['identity_type'], user['username'], user['domain'])
    rp.directory_user_by_user_key[key] = user
    rp.options['process_groups'] = True
    rp.push_umapi = True

    groups_to_add = {'Group A', 'Group C'}
    info = mock_umapi_info(None, {'Group A', 'Group B'})
    conn = MockUmapiConnector()
    rp.create_umapi_user(key, groups_to_add, info, conn)

    result = vars(conn.commands_sent)['do_list']
    result[0][1].pop('on_conflict')
    assert result[0] == ('create', {
        'email': user['email'],
        'first_name': user['firstname'],
        'last_name': user['lastname'],
        'country': user['country']})
    assert result[1] == ('remove_from_groups', {
        'groups': {'group b', 'group a'}})
    assert result[2] == ('add_to_groups', {
        'groups': {'Group C', 'Group A'}})


def test_update_umapi_user(rule_processor, mock_dir_user, mock_umapi_user, get_mock_user_list):
    rp = rule_processor
    user = mock_dir_user
    mock_umapi_user['email'] = user['email']
    mock_umapi_user['username'] = user['username']
    mock_umapi_user['domain'] = user['domain']
    mock_umapi_user['type'] = user['identity_type']

    def update(up_user, up_attrs):
        group_add = set()
        group_rem = set()
        conn = MockUmapiConnector()
        info = UmapiTargetInfo(None)
        user_key = rp.get_user_key(up_user['identity_type'], up_user['username'], up_user['domain'])
        rp.directory_user_by_user_key[user_key] = up_user
        rp.update_umapi_user(info, user_key, conn, up_attrs, group_add, group_rem, mock_umapi_user)
        assert user_key in rp.updated_user_keys
        return vars(conn.commands_sent)

    up_attrs = {
        'firstname': user['firstname'],
        'lastname': user['lastname']}

    result = update(user, up_attrs)
    assert result == {
        'identity_type': user['identity_type'],
        'email': user['email'],
        'username': user['username'],
        'domain': user['domain'],
        'do_list': [('update', {
            'first_name': user['firstname'],
            'last_name': user['lastname']})]}

    user['username'] = 'different@example.com'
    result = update(user, up_attrs)
    assert result['username'] == user['email']

    user['email'] = 'different@example.com'
    up_attrs = {
        'email': user['email']}
    result = update(user, up_attrs)

    assert result == {
        'identity_type': user['identity_type'],
        'email': user['email'],
        'username': user['username'],
        'domain': user['domain'],
        'do_list': [('update', {
            'email': 'different@example.com',
            'username': user['username']})]}


@mock.patch("user_sync.rules.RuleProcessor.update_umapi_user")
@mock.patch("user_sync.rules.RuleProcessor.add_stray")
def test_update_umapi_users_for_connector(add_stray, update_umapi_user, rule_processor, get_mock_user_list):
    rp = rule_processor
    rp.options['process_groups'] = True
    rp.options['update_user_info'] = True
    rp.will_process_strays = True

    conn = MockUmapiConnector()
    info = UmapiTargetInfo(None)
    info.add_mapped_group("New Group")
    info.add_mapped_group("To Remove")

    umapi_users = get_mock_user_list(count=6, umapi_users=True, groups=["Current Group", "To Remove"])
    dir_users = get_mock_user_list(groups=["Current Group", "New Group"])

    for k, u in six.iteritems(dir_users):
        u['firstname'] += " Updated Name"
        info.add_desired_group_for(k, "New Group")

    conn.users = umapi_users.values()
    rp.filtered_directory_user_by_user_key.update(dir_users)

    utg_map = rp.update_umapi_users_for_connector(info, conn)

    all_calls = [c[1] for c in update_umapi_user.mock_calls]
    all_calls = {c[1]: c for c in all_calls}

    # Sort the users for easier access when asserting
    dir_users = sorted(dir_users.values(), key=lambda u: u['email'])
    umapi_users = sorted(umapi_users.values(), key=lambda u: u['email'])

    # Generally check all the parameters passed
    # Just check the first call since all are the same
    assert utg_map == {}
    assert info.umapi_users_loaded
    assert len(all_calls) == len(umapi_users)

    c = all_calls[rp.get_umapi_user_key(umapi_users[0])]
    assert c[3] == {
        'firstname': dir_users[0]['firstname']}
    assert c[4] == {'new group'}
    assert c[5] == {'to remove'}
    assert c[6] == umapi_users[0]

    # Check that a stray is handled correctly
    strays = add_stray.mock_calls[1][1]
    assert strays == (None, rp.get_umapi_user_key(umapi_users[-1]), {"to remove"})


@mock.patch('user_sync.helper.CSVAdapter.read_csv_rows')
def test_stray_key_map(csv_reader, rule_processor):
    csv_mock_data = [
        {
            'type': 'adobeID',
            'username': 'removeuser2@example.com',
            'domain': 'example.com'},
        {
            'type': 'federatedID',
            'username': 'removeuser@example.com',
            'domain': 'example.com'},
        {
            'type': 'enterpriseID',
            'username': 'removeuser3@example.com',
            'domain': 'example.com'}
    ]

    csv_reader.return_value = csv_mock_data
    rule_processor.read_stray_key_map('')
    actual_value = rule_processor.stray_key_map
    expected_value = {
        None: {
            'federatedID,removeuser@example.com,': None,
            'enterpriseID,removeuser3@example.com,': None,
            'adobeID,removeuser2@example.com,': None}
    }

    assert expected_value == actual_value

    # Added secondary umapi value
    csv_mock_data = [
        {
            'type': 'adobeID',
            'username': 'remo@sample.com',
            'domain': 'sample.com',
            'umapi': 'secondary'},
        {
            'type': 'federatedID',
            'username': 'removeuser@example.com'},
        {
            'type': 'enterpriseID',
            'username': 'removeuser3@example.com',
            'domain': 'example.com'}

    ]
    csv_reader.return_value = csv_mock_data
    rule_processor.read_stray_key_map('')
    actual_value = rule_processor.stray_key_map
    expected_value = {
        'secondary': {
            'adobeID,remo@sample.com,': None
        },
        None: {
            'federatedID,removeuser@example.com,': None,
            'enterpriseID,removeuser3@example.com,': None,
            'adobeID,removeuser2@example.com,': None}
    }
    assert expected_value == actual_value


def test_get_user_attribute_difference(rule_processor, mock_dir_user, mock_umapi_user):
    directory_user_mock_data = mock_dir_user
    umapi_users_mock_data = mock_umapi_user
    umapi_users_mock_data['firstname'] = 'Adobe'
    umapi_users_mock_data['lastname'] = 'Username'
    umapi_users_mock_data['email'] = 'adobe.username@example.com'

    expected = {
        'email': mock_dir_user['email'],
        'firstname': mock_dir_user['firstname'],
        'lastname': mock_dir_user['lastname']
    }

    assert expected == rule_processor.get_user_attribute_difference(
        directory_user_mock_data, umapi_users_mock_data)

    # test with no change
    assert rule_processor.get_user_attribute_difference(
        umapi_users_mock_data, umapi_users_mock_data) == {}


def test_log_after_mapping_hook_scope(log_stream):
    stream, logger = log_stream

    def compare_attr(text, target):
        s = yaml.safe_load(re.search('{.+}', text).group())
        for attr in s:
            if s[attr] != 'None':
                assert s[attr] == target[attr]

    state = {
        'source_attributes': {
            'email': 'bsisko@example.com',
            'identity_type': None,
            'username': None,
            'domain': None,
            'givenName': 'Benjamin',
            'sn': 'Sisko',
            'c': 'CA'},
        'source_groups': set(),
        'target_attributes': {
            'email': 'bsisko@example.com',
            'username': 'bsisko@example.com',
            'domain': 'example.com',
            'firstname': 'Benjamin',
            'lastname': 'Sisko',
            'country': 'CA'},
        'target_groups': set(),
        'log_stream': logger,
        'hook_storage': None
    }

    ruleprocessor = RuleProcessor({})
    ruleprocessor.logger = logger
    ruleprocessor.after_mapping_hook_scope = state
    ruleprocessor.log_after_mapping_hook_scope(before_call=True)
    stream.flush()
    x = stream.getvalue().split('\n')

    assert len(x[2]) < 32
    assert len(x[4]) < 32
    compare_attr(x[1], state['source_attributes'])
    compare_attr(x[3], state['target_attributes'])

    state['target_groups'] = {'One'}
    state['target_attributes']['firstname'] = 'John'
    state['source_attributes']['sn'] = 'David'
    state['source_groups'] = {'One'}

    ruleprocessor.after_mapping_hook_scope = state
    ruleprocessor.log_after_mapping_hook_scope(after_call=True)
    stream.flush()
    x = stream.getvalue().split('\n')

    assert re.search('(Target groups, after).*(One)', x[6])
    compare_attr(x[5], state['target_attributes'])


def test_get_umapi_user_key(rule_processor):
    mock_umapi_user_dict = {
        'email': '7of9@exmaple.com',
        'username': '7of9@example.com',
        'domain': 'example.com',
        'type': 'federatedID'
    }

    actual_result = rule_processor.get_umapi_user_key(mock_umapi_user_dict)
    assert actual_result == 'federatedID,7of9@example.com,'


def test_get_user_key(rule_processor):
    key = rule_processor.get_user_key("federatedID", "wriker@example.com", "wriker@example.com", "example.com")
    assert key == 'federatedID,wriker@example.com,'

    key = rule_processor.get_user_key("federatedID", "wriker", "example.com")
    assert key == 'federatedID,wriker,example.com'

    assert not rule_processor.get_user_key(None, "wriker@example.com", "wriker@example.com", "example.com")
    assert not rule_processor.get_user_key("federatedID", None, "wriker@example.com")


def test_get_username_from_user_key(rule_processor):
    with mock.patch('user_sync.rules.RuleProcessor.parse_user_key') as parse:
        parse.return_value = ['federatedID', 'test_user@email.com', '']
        username = rule_processor.get_username_from_user_key("federatedID,test_user@email.com,")
        assert username == 'test_user@email.com'


def test_parse_user_key(rule_processor):
    parsed_user_key = rule_processor.parse_user_key("federatedID,test_user@email.com,")
    assert parsed_user_key == ['federatedID', 'test_user@email.com', '']

    domain_parsed_key = rule_processor.parse_user_key("federatedID,test_user,email.com")
    assert domain_parsed_key == ['federatedID', 'test_user', 'email.com']


def test_add_mapped_group():
    umapi_target_info = UmapiTargetInfo("")
    umapi_target_info.add_mapped_group("All Students")
    assert "all students" in umapi_target_info.mapped_groups
    assert "All Students" in umapi_target_info.non_normalize_mapped_groups


def test_add_additional_group():
    umapi_target_info = UmapiTargetInfo("")
    umapi_target_info.add_additional_group('old_name', 'new_name')
    assert umapi_target_info.additional_group_map['old_name'][0] == 'new_name'


def test_add_desired_group_for():
    umapi_target_info = UmapiTargetInfo("")
    with mock.patch("user_sync.rules.UmapiTargetInfo.get_desired_groups") as mock_desired_groups:
        mock_desired_groups.return_value = None
        umapi_target_info.add_desired_group_for('user_key', 'group_name')
        assert umapi_target_info.desired_groups_by_user_key['user_key'] == {'group_name'}


def test_create():
    with mock.patch("user_sync.rules.AdobeGroup._parse") as parse:
        parse.return_value = ('group_name', None)
        AdobeGroup.create('this')
        assert ('group_name', None) in AdobeGroup.index_map


def test_parse():
    result = AdobeGroup._parse('qualified_name')
    assert result == ('qualified_name', None)


def test_add_stray(rule_processor):
    user_key_mock_data = 'federatedID,rules.user@example.com,'
    removed_groups_mock_data = {'aishtest'}
    rule_processor.stray_key_map = {
        None: {}}
    rule_processor.add_stray(None, user_key_mock_data, removed_groups_mock_data)
    assert rule_processor.stray_key_map[None][user_key_mock_data] == removed_groups_mock_data


def test_is_selected_user_key(rule_processor):
    compiled_expression = re.compile(r'\A' + "nuver.yusser@example.com" + r'\Z', re.IGNORECASE)
    rule_processor.options['username_filter_regex'] = compiled_expression
    result = rule_processor.is_selected_user_key('federatedID,nuver.yusser@example.com,')
    assert result
    result = rule_processor.is_selected_user_key('federatedID,test@test.com,')
    assert not result
    compiled_expression = re.compile(r'\A' + ".*sser@example.com" + r'\Z', re.IGNORECASE)
    rule_processor.options['username_filter_regex'] = compiled_expression
    result = rule_processor.is_selected_user_key('federatedID,nuver.yusser@example.com,')
    assert result


def test_is_umapi_user_excluded(rule_processor):
    in_primary_org = True
    rule_processor.exclude_identity_types = ['adobeID']
    user_key = 'adobeID,adobe.user@example.com,'
    current_groups = {'default acrobat pro dc configuration', 'one', '_admin_group a'}
    assert rule_processor.is_umapi_user_excluded(in_primary_org, user_key, current_groups)

    user_key = 'federatedID,adobe.user@example.com,'
    rule_processor.exclude_groups = {'one'}
    assert rule_processor.is_umapi_user_excluded(in_primary_org, user_key, current_groups)

    user_key = 'federatedID,adobe.user@example.com,'
    rule_processor.exclude_groups = set()
    compiled_expression = re.compile(r'\A' + "adobe.user@example.com" + r'\Z', re.IGNORECASE)
    rule_processor.exclude_users = {compiled_expression}
    assert rule_processor.is_umapi_user_excluded(in_primary_org, user_key, current_groups)


def test_write_stray_key_map(rule_processor, tmpdir):
    tmp_file = str(tmpdir.join('strays_test.csv'))

    rule_processor.stray_list_output_path = tmp_file
    rule_processor.stray_key_map = {
        'secondary': {
            'adobeID,remoab@example.com,': set(),
            'enterpriseID,adobe.user3@example.com,': set(), },
        None: {

            'federatedID,adobe.only1@example.com,': set()}}
    assert result_user_groups_to_map == {
        'federatedID,directory.only1@example.com,': {'user_group'}}


def test_update_umapi_user(rule_processor, log_stream, mock_umapi_user):
    stream, logger = log_stream
    rule_processor.logger = logger

    mock_user_key = 'federatedID,both1@example.com,'
    mock_groups_to_add = {'added_user_group'}
    mock_groups_to_remove = {'removed_user_group'}
    mock_attributes_to_update = {
        'firstname': 'newfirstname',
        'email': 'newemail'
    }

    mock_umapi_user["groups"] = ["removed_user_group", "org"]
    mock_umapi_user['username'] = 'different@example.com'

    umapi_connector = mock.MagicMock()
    with mock.patch('user_sync.connector.umapi.Commands') as commands:
        commands.return_value = mock.MagicMock()
        rule_processor.update_umapi_user(UmapiTargetInfo(None), mock_user_key, umapi_connector,
                                         mock_attributes_to_update,
                                         mock_groups_to_add, mock_groups_to_remove, mock_umapi_user)
        commands_sent = str(umapi_connector.send_commands.call_args[0][0].method_calls)
        commands_sent = re.sub("set\\(\\[", "{", commands_sent)
        commands_sent = re.sub("\\]\\)", "}", commands_sent)
        assert "update_user" in commands_sent
        assert 'username' in commands_sent and "'firstname': 'newfirstname'" in commands_sent and "'email': 'newemail'" in commands_sent
        assert "remove_groups({'removed_user_group'})" in commands_sent
        assert "add_groups({'added_user_group'})" in commands_sent

    stream.flush()
    actual_logger_output = stream.getvalue()
    assert 'newfirstname' in actual_logger_output
    assert 'removed_user_group' in actual_logger_output
    assert 'added_user_group' in actual_logger_output
    assert mock_umapi_user["email"] == mock_umapi_user["username"]


def test_read_desired_user_groups(rule_processor, log_stream, mock_directory_user):
    rp = rule_processor
    stream, logger = log_stream
    rp.logger = logger
    mock_directory_user['groups'] = ['security_group']

    directory_connector = mock.MagicMock()
    directory_connector.load_users_and_groups.return_value = [mock_directory_user]
    mappings = {
        'security_group': [AdobeGroup.create('user_group')]
    }

    rp.read_desired_user_groups(mappings, directory_connector)

    # Assert the security group and adobe group end up in the correct scope
    assert "security_group" in rp.after_mapping_hook_scope['source_groups']
    assert "user_group" in rp.after_mapping_hook_scope['target_groups']

    # Assert the user group updated in umapi info
    user_key = rp.get_directory_user_key(mock_directory_user)
    assert ('user_group' in rp.umapi_info_by_name[None].desired_groups_by_user_key[user_key])

    # testing after_mapping_hooks
    AdobeGroup.create('existing_group')

    after_mapping_hook_text = """
first = source_attributes.get('givenName')
if first is not None: 
  target_groups.add('scope_group')
target_groups.add('existing_group')
"""

    hook_code = compile(after_mapping_hook_text, '<per-user after-mapping-hook>', 'exec')
    rp.options['after_mapping_hook'] = hook_code
    rp.read_desired_user_groups(mappings, directory_connector)
    stream.flush()
    logger_output = stream.getvalue()

    assert 'Target adobe group scope_group is not known; ignored' in logger_output
    assert rp.umapi_info_by_name[None].desired_groups_by_user_key == {
        user_key: {'user_group', 'existing_group'}
    }

    assert "security_group" in rp.after_mapping_hook_scope['source_groups']
    assert "existing_group" in rp.after_mapping_hook_scope['target_groups']

    # testing additional_groups
    rp.options['after_mapping_hook'] = None
    mock_directory_user['member_groups'] = ['other_security_group', 'security_group', 'more_security_group']
    rp.options['additional_groups'] = [
        {
            'source': re.compile('other(.+)'),
            'target': AdobeGroup.create('additional_user_group')},
        {
            'source': re.compile('security_group'),
            'target': AdobeGroup.create('additional(.+)')}]

    rp.read_desired_user_groups(mappings, directory_connector)
    assert 'other_security_group' in rp.umapi_info_by_name[None].additional_group_map[
        'additional_user_group']
    assert 'security_group' in rp.umapi_info_by_name[None].additional_group_map[
        'additional(.+)']
    assert {'additional_user_group', 'additional(.+)'}.issubset(
        rp.umapi_info_by_name[None].desired_groups_by_user_key[user_key])


@mock.patch("user_sync.rules.RuleProcessor.create_umapi_commands_for_directory_user")
def test_create_umapi_user(create_commands, rule_processor):
    rule_processor.directory_user_by_user_key['test'] = 'test'

    mock_command = MagicMock()
    create_commands.return_value = mock_command
    rule_processor.options['process_groups'] = True
    rule_processor.push_umapi = True
    rule_processor.create_umapi_user('test', set(), MagicMock(), MagicMock())

    called = [c[0] for c in mock_command.mock_calls][1:]
    assert called == ['remove_groups', 'add_groups']


@pytest.fixture
def mock_user_directory_data():
    return {
        'federatedID,both1@example.com,':
            {
                'identity_type': 'federatedID',
                'username': 'both1@example.com',
                'domain': 'example.com',
                'firstname': 'both1',
                'lastname': 'one',
                'email': 'both1@example.com',
                'groups': ['All Sea of Carag'],
                'country': 'US',
                'member_groups': [],
                'source_attributes': {
                    'email': 'both1@example.com',
                    'identity_type': None,
                    'username': None,
                    'domain': None,
                    'givenName': 'both1',
                    'sn': 'one',
                    'c': 'US'}},
        'federatedID,both2@example.com,':
            {
                'identity_type': 'federatedID',
                'username': 'both2@example.com',
                'domain': 'example.com',
                'firstname': 'both2',
                'lastname': 'one',
                'email': 'both2@example.com',
                'groups': ['All Sea of Carag'],
                'country': 'US',
                'member_groups': [],
                'source_attributes': {
                    'email': 'both2@example.com',
                    'identity_type': None,
                    'username': None,
                    'domain': None,
                    'givenName': 'both2',
                    'sn': 'two',
                    'c': 'US'}},
        'federatedID,both3@example.com,':
            {
                'identity_type': 'federatedID',
                'username': 'both3@example.com',
                'domain': 'example.com',
                'firstname': 'both3',
                'lastname': 'one',
                'email': 'both3@example.com',
                'groups': ['All Sea of Carag'],
                'country': 'US',
                'member_groups': [],
                'source_attributes': {
                    'email': 'both3@example.com',
                    'identity_type': None,
                    'username': None,
                    'domain': None,
                    'givenName': 'both3',
                    'sn': 'three',
                    'c': 'US'}},
        'federatedID,directory.only1@example.com,':
            {
                'identity_type': 'federatedID',
                'username': 'directory.only1@example.com',
                'domain': 'example.com',
                'firstname': 'dir1',
                'lastname': 'one',
                'email': 'directory.only1example.com',
                'groups': ['All Sea of Carag'],
                'country': 'US',
                'member_groups': [],
                'source_attributes': {
                    'email': 'directory.only1@example.com',
                    'identity_type': None,
                    'username': None,
                    'domain': None,
                    'givenName': 'dir1',
                    'sn': 'one',
                    'c': 'US'}}
    }


@pytest.fixture
def mock_umapi_user_data():
    return [
        {
            'email': 'both1@example.com',
            'status': 'active',
            'groups': ['_org_admin', 'group1'],
            'username': 'both1@example.com',
            'adminRoles': ['org'],
            'domain': 'example.com',
            'country': 'US',
            'type': 'federatedID'},
        {
            'email': 'both2@example.com',
            'status': 'active',
            'groups': ['_org_admin', 'user_group'],
            'username': 'both2@example.com',
            'adminRoles': ['org'],
            'domain': 'example.com',
            'country': 'US',
            'type': 'federatedID'},
        {
            'email': 'both3@example.com',
            'status': 'active',
            'groups': ['_org_admin', 'group1', 'user_group'],
            'username': 'both3@example.com',
            'adminRoles': ['org'],
            'domain': 'example.com',
            'country': 'US',
            'type': 'federatedID'},
        {
            'email': 'adobe.only1@example.com',
            'status': 'active',
            'groups': ['_org_admin'],
            'username': 'adobe.only1@example.com',
            'adminRoles': ['org'],
            'domain': 'example.com',
            'country': 'US',
            'type': 'federatedID'},
        {
            'email': 'exclude1@example.com',
            'status': 'active',
            'groups': ['_org_admin'],
            'username': 'exclude1@example.com',
            'adminRoles': ['org'],
            'domain': 'example.com',
            'country': 'US',
            'type': 'federatedID'}]


@pytest.fixture
def rule_processor(caller_options):
    return RuleProcessor(caller_options)


@pytest.fixture
def caller_options():
    return {
        'adobe_group_filter': None,
        'after_mapping_hook': None,
        'default_country_code': 'US',
        'delete_strays': False,
        'directory_group_filter': None,
        'disentitle_strays': False,
        'exclude_groups': [],
        'exclude_identity_types': ['adobeID'],
        'exclude_strays': False,
        'exclude_users': [],
        'extended_attributes': None,
        'process_groups': True,
        'max_adobe_only_users': 200,
        'new_account_type': 'federatedID',
        'remove_strays': True,
        'strategy': 'sync',
        'stray_list_input_path': None,
        'stray_list_output_path': None,
        'test_mode': True,
        'update_user_info': False,
        'username_filter_regex': None,
        'adobe_only_user_action': ['remove'],
        'adobe_only_user_list': None,
        'adobe_users': ['all'],
        'config_filename': 'tests/fixture/user-sync-config.yml',
        'connector': 'ldap',
        'encoding_name': 'utf8',
        'user_filter': None,
        'users': None,
        'directory_connector_type': 'csv',
        'directory_connector_overridden_options': {
            'file_path': '../tests/fixture/remove-data.csv'},
        'adobe_group_mapped': False,
        'additional_groups': []}
    called = [c[0] for c in mock_command.mock_calls][1:]
    assert called == ['remove_groups', 'add_groups']


@mock.patch("user_sync.connector.umapi.Commands")
def test_manage_strays(commands, rule_processor):
    commands.return_value = mock.MagicMock()
    umapi_connector = mock.MagicMock()
    umapi_connector.get_primary_connector.return_value = mock.MagicMock()
    rule_processor.stray_key_map = {None: {'federatedID,example@email.com,': {'user_group'}}}

    rule_processor.options['disentitle_strays'] = True
    rule_processor.manage_strays(umapi_connector)
    called = [c[0] for c in commands.mock_calls]
    assert '().remove_all_groups' in called

    rule_processor.options['disentitle_strays'] = False
    commands.mock_calls = []
    rule_processor.options['remove_strays'] = True
    rule_processor.manage_strays(umapi_connector)
    called = [c[0] for c in commands.mock_calls]

    assert '().remove_from_org' in called

    rule_processor.options['remove_strays'] = False

    commands.mock_calls = []
    rule_processor.options['delete_strays'] = True
    rule_processor.manage_strays(umapi_connector)
    called = [c[0] for c in commands.mock_calls]
    assert '().remove_from_org' in called

    rule_processor.options['disentitle_strays'] = False
    rule_processor.options['remove_strays'] = False
    rule_processor.options['delete_strays'] = False
    commands.mock_calls = []
    rule_processor.manage_stray_groups = True
    rule_processor.manage_strays(umapi_connector)
    called = [c[0] for c in commands.mock_calls]
    assert '().remove_groups' in called




    rule_processor.manage_strays(umapi_connector)

            'enterpriseID,adobe.user1@example.com,': set(),
            'federatedID,adobe.user2@example.com,': set()
        }}

    rule_processor.write_stray_key_map()
    with open(tmp_file, 'r') as our_file:
        reader = csv.reader(our_file)
        actual = list(reader)
        expected = [['type', 'username', 'domain', 'umapi'],
                    ['adobeID', 'remoab@example.com', '', 'secondary'],
                    ['enterpriseID', 'adobe.user3@example.com', '', 'secondary'],
                    ['enterpriseID', 'adobe.user1@example.com', '', ''],
                    ['federatedID', 'adobe.user2@example.com', '', '']]
        assert compare_iter(actual, expected)

