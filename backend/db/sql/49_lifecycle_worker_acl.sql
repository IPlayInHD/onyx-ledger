-- Entry 11B5J — the application runtime must not drive the deletion lifecycle.
--
-- THE DEFECT, found by the pristine-database certification and reproduced from a
-- genuine runtime LOGIN (session_user = onyx_test, a member of onyx_app_rw and
-- nothing else):
--
--   SELECT out_user_id, out_state, out_claim_token
--     FROM identity.claim_account_lifecycle(10, 'attacker');
--   -- returned ANOTHER TENANT'S user_id, state and claim token
--
--   SELECT identity.advance_account_lifecycle(
--            '<victim>', '<token just obtained>', 'ACCESS_DISABLED', 'attacker');
--   -- t   → the victim's account is now ACCESS_DISABLED
--
-- So an ordinary request-path session could enumerate accounts pending deletion
-- across tenants and disable any of them. It could not purge — EXECUTE on
-- `identity.purge_source_data` is correctly denied to onyx_app_rw — which bounds
-- the impact to cross-tenant identifier disclosure and denial of service rather
-- than data destruction.
--
-- WHY IT SURVIVED PD-16. PD-16 was about `GRANT onyx_freshness_worker TO
-- onyx_app_rw` — a ROLE MEMBERSHIP — and its remediation revoked exactly that.
-- These are DIRECT function grants on a different family, written earlier under
-- a different deployment assumption, and no gate looked at them. The grant even
-- documented the assumption in `41_account_lifecycle.sql`:
--
--   "The application role runs the worker in this deployment shape, so it needs
--    the same EXECUTE."
--
-- That shape is precisely what Entry 11B5E replaced. Since 11B5E5 the privacy
-- worker authenticates as its own LOGIN through `privacy_unit_of_work()`, and
-- the comment describes a topology that no longer exists.
--
-- THE APPLICATION DOES NOT NEED THESE. The only caller of all three is
-- `workers/tasks/privacy.py`, which opens `privacy_unit_of_work()`. The
-- app-engine call sites of `AccountLifecycleService` use `request_deletion`,
-- `status` and `assert_may_act` — none of which is one of these keyholes.
--
-- `identity.account_deletion_state(uuid)` KEEPS its grant: it is the ordinary
-- read the request path legitimately performs about its OWN account, not a
-- cross-tenant worker capability.
REVOKE EXECUTE ON FUNCTION identity.claim_account_lifecycle(integer, text)
    FROM onyx_app_rw;
REVOKE EXECUTE ON FUNCTION identity.advance_account_lifecycle(uuid, uuid, text, text)
    FROM onyx_app_rw;
REVOKE EXECUTE ON FUNCTION identity.fail_account_lifecycle(uuid, uuid, text, text)
    FROM onyx_app_rw;

COMMENT ON FUNCTION identity.claim_account_lifecycle(integer, text) IS
    'Privileged worker keyhole: claims accounts owed lifecycle work across tenants and returns their claim tokens. EXECUTE belongs to onyx_privacy_worker alone — the application runtime held it until Entry 11B5J and could enumerate and disable other tenants'' accounts.';
