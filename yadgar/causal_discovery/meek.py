"""Meek orientation rules for the PC algorithm."""

import logging

logger = logging.getLogger(__name__)


def meek_r1(
    i: int,
    j: int,
    n_vars: int,
    adjacency: list[list[bool]],
    directed: list[list[bool]],
) -> bool:
    """Meek R1 (non-collider): if X->i and i-j and X not adj j, orient i->j.

    Returns True if the edge was oriented.
    """
    for x in range(n_vars):
        if directed[x][i] and not adjacency[x][j]:
            directed[i][j] = True
            return True
    return False


def meek_r2(
    i: int,
    j: int,
    n_vars: int,
    adjacency: list[list[bool]],
    directed: list[list[bool]],
) -> bool:
    """Meek R2 (acyclicity): if i->z->j and i-j undirected, orient i->j.

    Returns True if the edge was oriented.
    """
    for z in range(n_vars):
        if directed[i][z] and directed[z][j]:
            directed[i][j] = True
            return True
    return False


def meek_r3(
    i: int,
    j: int,
    n_vars: int,
    adjacency: list[list[bool]],
    directed: list[list[bool]],
) -> bool:
    """Meek R3 (non-adjacent): if i-z1, i-z2, z1->j, z2->j, z1 not adj z2, orient i->j.

    Returns True if the edge was oriented.
    """
    z_to_y = [z for z in range(n_vars) if z != i and z != j and adjacency[i][z] and directed[z][j]]
    valid_pairs = any(
        not adjacency[z1][z2] for idx1, z1 in enumerate(z_to_y) for z2 in z_to_y[idx1 + 1 :]
    )
    if len(z_to_y) >= 2 and valid_pairs:
        directed[i][j] = True
        return True
    return False
