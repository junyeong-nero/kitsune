"""Keep bridge credentials private on POSIX and native Windows filesystems."""

import os
import sys

_warned = set()  # directories whose unchanged ACL this process already reported


def _principals():
    """SIDs of the current user, SYSTEM and Administrators: the only ones a private ACL allows."""
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        owner = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    return (owner,
            win32security.CreateWellKnownSid(win32security.WinLocalSystemSid),
            win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid))


def _windows_acl(path: str, *, directory: bool) -> None:
    import ntsecuritycon
    import win32con
    import win32security

    acl = win32security.ACL()
    inheritance = (win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE) if directory else 0
    for principal in _principals():
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, inheritance,
                                 ntsecuritycon.FILE_ALL_ACCESS, principal)
    win32security.SetNamedSecurityInfo(
        path, win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, acl, None,
    )


def _has_private_acl(path: str) -> bool:
    """Whether path already has the protected ACL _windows_acl sets."""
    import win32security

    descriptor = win32security.GetNamedSecurityInfo(
        path, win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION)
    if not descriptor.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED:
        return False
    acl = descriptor.GetSecurityDescriptorDacl()
    if acl is None:
        return False
    allowed = {win32security.ConvertSidToStringSid(sid) for sid in _principals()}
    return {win32security.ConvertSidToStringSid(acl.GetAce(i)[2]) for i in range(acl.GetAceCount())} == allowed


def ensure_directory(path: str) -> None:
    """Create the bridge's data directory, private to the current user.

    On Windows, chmod(0700) does not remove inherited access for other users, so the directory
    gets a protected ACL, and startup fails if the filesystem cannot enforce it. That ACL replaces
    the directory's own, so it is set only on a directory the bridge creates or finds empty: a
    KITSUNE_HOME pointed at a folder that already holds other files keeps its ACL, with a warning.
    The token file gets its own protected ACL either way (secure_file).
    """
    try:
        os.makedirs(path, mode=0o700)
        created = True
    except FileExistsError:
        if not os.path.isdir(path):
            raise
        created = False
    if os.name != "nt":
        return
    if created or not os.listdir(path):
        _windows_acl(path, directory=True)
    elif not _has_private_acl(path) and path not in _warned:
        _warned.add(path)
        print(f"Kitsune: {path} already holds other files, so its permissions were left unchanged and "
              "only the token file is private to you. Set KITSUNE_HOME to a new or empty directory to "
              "keep the bridge's log and state private too.", file=sys.stderr)


def secure_file(path: str) -> None:
    if os.name == "nt":
        _windows_acl(path, directory=False)
