"""Compensation faunistique.

CESBIO (`ecocompensation.vegetation_sur_cesbio`) et faune (`ecocompensation.fauna`)
sont des tables nationales : filter_v2 les interroge en SQL direct
(EXISTS / ST_DWithin), sans clip AOI dans ce dossier.
"""
