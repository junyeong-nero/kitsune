"""Keep bridge credentials private on POSIX and native Windows filesystems."""

import os


def _windows_acl(path: str, *, directory: bool) -> None:
    import ntsecuritycon
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        owner = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    acl = win32security.ACL()
    inheritance = (win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE) if directory else 0
    principals = (owner,
                  win32security.CreateWellKnownSid(win32security.WinLocalSystemSid),
                  win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid))
    for principal in principals:
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, inheritance,
                                 ntsecuritycon.FILE_ALL_ACCESS, principal)
    win32security.SetNamedSecurityInfo(
        path, win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, acl, None,
    )


def ensure_directory(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    if os.name == "nt":
        # chmod(0700) on Windows does not remove inherited access for other users.
        # Fail startup if the filesystem cannot enforce this credential boundary.
        _windows_acl(path, directory=True)


def secure_file(path: str) -> None:
    if os.name == "nt":
        _windows_acl(path, directory=False)
